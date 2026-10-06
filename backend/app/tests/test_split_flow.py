"""拆袋互证：总表有几行、回包打谁、收走列谁、履历回放，都指向留下的那批真实行。

种子数据（每个用例独立 tmp 库）：
  lot1 牛奶×2 2026-10-01 clean · lot2 牛奶×1 2026-09-28 clean
  lot3 鸡蛋×12 2026-11-01 clean · lot4 冻饺×1 2025-01-01 dirty
  lot5 鸡蛋×-3 2026-12-01 dirty（负余量脏行）
"""
import json

from app.db import connect
from app.engines import split_reply

LAYERS = ("upper", "mid", "lower")


def fridge(client, layer=None):
    q = "?layer=" + layer if layer else ""
    r = client.get("/api/fridge" + q)
    assert r.status_code == 200
    return r.json()


def total(rows):
    return sum(float(r["qty_remain"]) for r in rows)


def lot(db, lot_id):
    return dict(db.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone())


def confirm(client, lot_id, qty):
    return client.post("/api/splits/confirm", json={"lot_id": lot_id, "qty": qty})


# --- 预览不改数字 -----------------------------------------------------------

def test_preview_never_mutates(client):
    before = fridge(client)
    r = client.post("/api/splits/preview", json={"lot_id": 3, "qty": 5})
    assert r.status_code == 200
    p = r.json()
    assert (p["before"], p["parent_after"], p["child_qty"]) == (12, 7, 5)
    assert p["sum_after"] == 12 and p["conserved"] is True
    assert fridge(client) == before  # 预览纯计算，总表一行一数未动


# --- 拆量为零或大于余量：整单失败，两页都不多行 ------------------------------

def test_split_qty_zero_or_over_remain_fails_wholesale(client):
    rows0, total0 = fridge(client), total(fridge(client))
    mid0 = fridge(client, "mid")

    assert confirm(client, 3, 0).status_code == 400            # 拆量为零
    assert confirm(client, 3, -1).status_code == 400           # 拆量为负
    r = confirm(client, 3, 13)                                 # 拆量大于余量
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "split_exceeds_remain"

    # 整单失败：总表与分层页同一收口——都不多行、数字不变
    assert fridge(client) == rows0
    assert fridge(client, "mid") == mid0
    assert total(fridge(client)) == total0


def test_failed_split_never_leaves_extra_rows(client):
    n0 = len(fridge(client))
    counts0 = {L: len(fridge(client, L)) for L in LAYERS}

    assert confirm(client, 3, 0).status_code == 400                     # 零
    assert confirm(client, 3, 99).status_code == 409                    # 超余量
    assert confirm(client, 4, 1).status_code == 409                     # 脏行
    assert confirm(client, 5, 1).status_code == 409                     # 负余量行
    assert confirm(client, 999, 1).status_code == 404                   # 不存在的批

    # 不能一页多一行：两页行数都回到拆前
    assert len(fridge(client)) == n0
    assert {L: len(fridge(client, L)) for L in LAYERS} == counts0


# --- 确认后：母行与子行并存，两页数字加起来等于拆前之和 ----------------------

def test_confirm_conserves_and_both_pages_agree(client):
    rows0, total0 = fridge(client), total(fridge(client))
    mid0 = total(fridge(client, "mid"))

    body = confirm(client, 3, 5).json()
    assert (body["before"], body["parent_after"], body["child_qty"]) == (12, 7, 5)
    assert body["sum_after"] == 12
    child_id = body["child_id"]

    rows1 = fridge(client)
    assert len(rows1) == len(rows0) + 1                       # 总表多一行：子批
    parent = next(r for r in rows1 if r["id"] == 3)
    child = next(r for r in rows1 if r["id"] == child_id)
    assert parent["qty_remain"] == 7                          # 母行余量已减
    assert child["qty_remain"] == 5 and child["split_from"] == 3

    # 从总表或分层页点拆，做成后两页数字加起来等于拆前之和
    assert total(rows1) == total0
    assert total(fridge(client, "mid")) == mid0
    assert total(rows1) == sum(total(fridge(client, L)) for L in LAYERS)


# --- 回包打谁、总表行身份：都指向留下的真实行 --------------------------------

def test_reply_and_fridge_rows_point_at_real_lots(client):
    body = confirm(client, 3, 5).json()
    # 拆袋这一笔真实扣在母批：回包与履历同记母批，不二次归因到子批
    assert body["consume_lot_id"] == 3
    assert body["history_lot_id"] == 3
    assert body["deductions"] == [{"lot_id": 3, "take": 5}]
    # 总表每一行：行上动作命中行自己的 id（子行不再挂母批身份）
    for row in fridge(client):
        assert row["hit_id"] == row["id"]
        assert row["shown_id"] == row["id"]


# --- 随后先到期应打到拆出来的行 ----------------------------------------------

def test_fefo_hits_the_split_out_child(client):
    child_id = confirm(client, 3, 5).json()["child_id"]

    # 同到期按 id 先后：母批 7 扣完后，第 8 个必须打到拆出来的子批
    r = client.post("/api/consume", json={"item_id": 2, "qty": 8})
    assert r.status_code == 200
    assert [(d["lot_id"], d["take"]) for d in r.json()["deductions"]] == [(3, 7), (child_id, 1)]

    db = connect()
    assert lot(db, 3)["status"] == "consumed" and lot(db, 3)["qty_remain"] == 0
    assert lot(db, child_id)["qty_remain"] == 4

    # 子批是真实行：再扣直接命中它自己
    r = client.post("/api/consume", json={"item_id": 2, "qty": 4})
    assert [d["lot_id"] for d in r.json()["deductions"]] == [child_id]
    assert lot(db, child_id)["status"] == "consumed"
    db.close()


def test_full_split_parent_retires_from_candidates(client):
    body = confirm(client, 3, 12).json()                      # 全拆
    child_id = body["child_id"]
    assert body["parent_after"] == 0

    # 母批余量 0：不能再拆、不再进扣减候选；但行仍在总表（母行与子行并存）
    r = confirm(client, 3, 1)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "lot_not_eligible"
    r = client.post("/api/consume", json={"item_id": 2, "qty": 2})
    assert [d["lot_id"] for d in r.json()["deductions"]] == [child_id]
    ids = {r["id"] for r in fridge(client)}
    assert {3, child_id} <= ids


# --- 脏行、负余量行：能不能拆 = 能不能进扣减候选，同一套资格 ------------------

def test_dirty_and_negative_rows_share_one_eligibility(client):
    # 脏行：不能拆，也进不了扣减候选
    r = confirm(client, 4, 1)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "lot_not_eligible"
    r = client.post("/api/consume", json={"item_id": 3, "qty": 1})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "short"

    # 负余量行：不能拆；扣鸡蛋只命中干净的 3 号批，负行不沾
    r = confirm(client, 5, 1)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "lot_not_eligible"
    r = client.post("/api/consume", json={"item_id": 2, "qty": 1})
    assert [d["lot_id"] for d in r.json()["deductions"]] == [3]

    # 预览与确认同一套资格
    r = client.post("/api/splits/preview", json={"lot_id": 4, "qty": 1})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "lot_not_eligible"


def test_consume_qty_non_positive_rejected(client):
    assert client.post("/api/consume", json={"item_id": 2, "qty": 0}).status_code == 400


# --- 收走列谁：真实到期的行自己 ------------------------------------------------

def test_sweep_takes_the_real_expired_rows(client):
    # 入一批昨天到期的干净货并拆出子批：母与子都真实到期
    lot_id = client.post("/api/lots", json={"item_id": 1, "qty": 3, "expiry": "2026-10-05"}).json()["id"]
    child_id = confirm(client, lot_id, 1).json()["child_id"]

    swept = set(client.post("/api/expire-sweep").json()["expired_ids"])
    # 收走的是留下的那批真实行：母批和子批各自在其列，不是只收母批身份
    assert {lot_id, child_id} <= swept

    db = connect()
    assert lot(db, lot_id)["status"] == "expired"
    assert lot(db, child_id)["status"] == "expired"
    db.close()
    remaining = {r["id"] for r in fridge(client)}
    assert lot_id not in remaining and child_id not in remaining


# --- 履历回看：回放与总表逐行对平，不按子行再扣一遍 ---------------------------

def test_history_replay_reconciles_with_shelf(client):
    body = confirm(client, 3, 5).json()
    split_out = {3: body["child_qty"]}                        # 母批被拆出的量
    client.post("/api/consume", json={"item_id": 2, "qty": 8})
    client.post("/api/consume", json={"item_id": 2, "qty": 1})

    db = connect()
    consumed = {}
    for row in db.execute("SELECT result_json FROM consumptions"):
        for d in json.loads(row["result_json"])["deductions"]:
            consumed[d["lot_id"]] = consumed.get(d["lot_id"], 0) + d["take"]
    # 任一行：qty_in - 现余量 == 履历扣减 + 拆出量（履历不双扣、不漏记）
    for r in db.execute("SELECT * FROM lots"):
        r = dict(r)
        expect = r["qty_in"] - consumed.get(r["id"], 0) - split_out.get(r["id"], 0)
        assert abs(r["qty_remain"] - expect) < 1e-9, f"lot {r['id']} 履历与在架对不平"
    db.close()


# --- 身份缝本身：纯函数层面钉住「都指向真实行」 -------------------------------

def test_split_reply_seam_points_at_real_rows():
    assert split_reply.consume_target_id(3, 9) == 3           # 回包打真实被扣的母批
    assert split_reply.history_target_id(3, 9) == 3           # 履历与回包同源
    rows = [{"id": 9, "split_from": 3}, {"id": 3, "split_from": None}]
    assert [r["hit_id"] for r in split_reply.annotate_fridge(rows)] == [9, 3]
    lots = [{"id": 9, "split_from": 3, "expiry": "2026-01-01", "qty_remain": 1}]
    assert split_reply.sweep_ids(lots, "2026-10-06", lambda ls, t: [9]) == [9]
    assert "split_from" not in split_reply.eligibility_sql()  # 子批同权进候选
