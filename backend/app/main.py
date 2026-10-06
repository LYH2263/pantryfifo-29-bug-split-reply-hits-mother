import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots, split_plan
from app.engines import split_reply

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = split_reply.annotate_fridge([dict(r) for r in c.execute(q, args)])
    c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return split_reply.alerts_rows(out)

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        # 资格与分装母批同一套（split_reply.eligibility_sql）：子批同权进 FEFO 候选
        lots = [dict(r) for r in c.execute(split_reply.eligibility_sql(), (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            raise HTTPException(400, result["reason"])
        if not result["ok"]:
            raise HTTPException(409, result)
        for d in result["deductions"]:
            # 守卫式扣减：拆批/过期可能在计划与落库之间改变行；余量不够则整单回滚，
            # 扣减只落在计划命中的真实行（回包打谁 = 行自己的 id）。
            cur = c.execute(
                "UPDATE lots SET qty_remain = qty_remain - ? "
                "WHERE id=? AND status='on_shelf' AND data_quality='clean' AND qty_remain >= ?",
                (d["take"], d["lot_id"], d["take"]))
            if cur.rowcount != 1:
                raise HTTPException(409, {"ok": False, "reason": "lot_changed", "lot_id": d["lot_id"]})
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                cur = c.execute(
                    "UPDATE lots SET status='consumed', qty_remain=0 "
                    "WHERE id=? AND status='on_shelf' AND qty_remain <= 0", (d["lot_id"],))
                if cur.rowcount != 1:
                    raise HTTPException(409, {"ok": False, "reason": "lot_changed", "lot_id": d["lot_id"]})
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit()
    except Exception:
        # 任一步失败整单回滚：不留下扣了一半的母行/子行
        c.rollback(); raise
    finally:
        c.close()
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    try:
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = split_reply.sweep_ids(lots, date.today().isoformat(), expire_lots)
        changed = []
        for i in ids:
            # 收走列谁 = 真实到期的行自己；已消费/余量为 0 的行不会被误收
            cur = c.execute(
                "UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf' AND qty_remain>0", (i,))
            if cur.rowcount == 1:
                changed.append(i)
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return {"expired_ids": changed}

class SplitIn(BaseModel):
    lot_id: int
    qty: float

def _load_lot(c, lot_id: int) -> dict | None:
    r = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    return dict(r) if r else None

def _split_preview_payload(lot: dict, qty: float) -> dict:
    plan = split_plan(lot, qty)
    if not plan["ok"]:
        return plan
    return {
        "ok": True,
        "parent_id": lot["id"],
        "item_id": lot["item_id"],
        "qty": plan["qty"],
        "before": plan["before"],
        "parent_after": plan["parent_after"],
        "child_qty": plan["child_qty"],
        "expiry": plan["expiry"],
        # 拆前 == 拆后（母批余量 + 子批）
        "sum_after": plan["parent_after"] + plan["child_qty"],
        "conserved": abs(plan["parent_after"] + plan["child_qty"] - plan["before"]) <= 1e-9,
    }

@app.post("/api/splits/preview")
def split_preview(body: SplitIn):
    """预览：纯计算，不写库、不改母批余量。"""
    c = connect()
    lot = _load_lot(c, body.lot_id)
    if not lot: c.close(); raise HTTPException(404, "lot")
    payload = _split_preview_payload(lot, body.qty)
    c.close()
    if not payload["ok"]:
        if payload["reason"] == "split_qty_non_positive":
            raise HTTPException(400, payload["reason"])
        raise HTTPException(409, payload)
    return payload

@app.post("/api/splits/confirm")
def split_confirm(body: SplitIn):
    """确认分装：母批减余量、子批上架，同一事务原子提交。

    - 守恒：母批拆后余量 + 子批量 == 拆前余量
    - 子批 split_from 指向母批；母批行永不消失，后续 FEFO 命中的是真实子批 id
    - 任一步失败整单回滚，绝不留下没有母批的孤儿行
    """
    c = connect()
    try:
        lot = _load_lot(c, body.lot_id)
        if not lot: raise HTTPException(404, "lot")
        before = float(lot["qty_remain"])
        payload = _split_preview_payload(lot, body.qty)
        if not payload["ok"]:
            if payload["reason"] == "split_qty_non_positive":
                raise HTTPException(400, payload["reason"])
            raise HTTPException(409, payload)
        q = payload["qty"]
        # 守卫式扣减母批：资格与消费同一套，且余量必须仍等于预览值——
        # 拆袋确认与扣减/收走叠在同一母行时只有一个能成交，其余 409 重新预览，
        # 绝不出现子行已上架而母行未减、或母行已减而子行没有的半状态。
        cur = c.execute(
            "UPDATE lots SET qty_remain = qty_remain - ? "
            "WHERE id=? AND status='on_shelf' AND data_quality='clean' AND qty_remain = ?",
            (q, body.lot_id, before))
        if cur.rowcount != 1:
            raise HTTPException(409, {"ok": False, "reason": "lot_changed", "lot_id": body.lot_id})
        parent_after = c.execute(
            "SELECT qty_remain FROM lots WHERE id=?", (body.lot_id,)).fetchone()["qty_remain"]
        # 子批先落内存校验守恒，再插入；插入随事务一起提交
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality,split_from) "
            "VALUES (?,?,?,?,?,?,?)",
            (lot["item_id"], q, q, lot["expiry"], "on_shelf", "clean", body.lot_id))
        child_id = cur.lastrowid
        check = c.execute(
            "SELECT COALESCE(SUM(qty_remain),0) s FROM lots WHERE id IN (?,?)",
            (body.lot_id, child_id)).fetchone()["s"]
        if abs(float(check) - before) > 1e-9:
            raise HTTPException(500, "split_invariant_violation")
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    c.close()
    # 回包与履历同记真实被扣的母批；子批是独立新行，不再被二次归因
    return {
        "ok": True, "parent_id": body.lot_id, "child_id": child_id,
        "consume_lot_id": split_reply.consume_target_id(body.lot_id, child_id),
        "history_lot_id": split_reply.history_target_id(body.lot_id, child_id),
        "before": before, "parent_after": parent_after, "child_qty": q,
        "expiry": lot["expiry"], "sum_after": float(check),
        "deductions": split_reply.rewrite_deductions(
            [{"lot_id": child_id, "take": q}], body.lot_id, child_id),
    }

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
