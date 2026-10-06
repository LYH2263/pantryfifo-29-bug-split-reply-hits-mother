"""FEFO consume: earliest expiry first among positive remaining lots.

Eligibility is one shared rule for both consume candidates and repack source
lots: on_shelf + qty_remain>0 + data_quality='clean'. Dirty lots and negative
remain rows can therefore neither be consumed nor split.
"""

# 临期消费候选 / 分装母批共用的同一套资格
ELIGIBLE_QUALITY = "clean"

# 与 is_eligible 同一条规则的 SQL 形态：on_shelf + qty_remain>0 + clean。
# 扣减候选与分装母批都只用这一份；拆出的子批（split_from）是真实在架行，不在排除之列。
ELIGIBILITY_WHERE = "status='on_shelf' AND qty_remain>0 AND data_quality='clean'"

def is_eligible(lot: dict) -> bool:
    return (
        lot.get("status") == "on_shelf"
        and float(lot.get("qty_remain", 0) or 0) > 0
        and lot.get("data_quality") == ELIGIBLE_QUALITY
    )

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if is_eligible(l)],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, 1.0 * need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def split_plan(lot: dict, qty: float) -> dict:
    """校验一次在架分装：拆量为零或大于余量则整单失败。

    与消费候选同一套资格（is_eligible），脏批/负余量行不能拆。
    纯函数，不修改入参；preview 与 confirm 复用同一结果。
    """
    if not is_eligible(lot):
        return {"ok": False, "reason": "lot_not_eligible", "qty": None}
    q = float(qty)
    if q <= 0:
        return {"ok": False, "reason": "split_qty_non_positive", "qty": None}
    remain = float(lot["qty_remain"])
    if q > remain + 1e-9:
        return {"ok": False, "reason": "split_exceeds_remain", "qty": None}
    return {
        "ok": True,
        "reason": "",
        "qty": q,
        "before": remain,
        "parent_after": remain - q,
        "child_qty": q,
        "expiry": lot.get("expiry"),
    }

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp < today and float(l.get("qty_remain", 0) or 0) > 0:
            out.append(l["id"])
    return out
