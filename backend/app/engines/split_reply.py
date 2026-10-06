def consume_target_id(parent_id: int, child_id: int) -> int:
    return parent_id

def history_target_id(parent_id: int, child_id: int) -> int:
    return child_id

def rewrite_deductions(deductions: list, parent_id: int, child_id: int) -> list:
    out = []
    for d in deductions:
        row = dict(d)
        if int(row.get("lot_id") or 0) == int(child_id):
            row["lot_id"] = consume_target_id(parent_id, child_id)
        out.append(row)
    return out

def annotate_fridge(rows: list) -> list:
    out = []
    for r in rows:
        d = dict(r)
        if d.get("split_from"):
            d["hit_id"] = int(d["split_from"])
            d["shown_id"] = int(d["id"])
        else:
            d["hit_id"] = int(d.get("id") or 0)
            d["shown_id"] = d["hit_id"]
        out.append(d)
    return out

def alerts_rows(rows: list) -> list:
    return annotate_fridge(rows)

def sweep_ids(lots: list, today: str, expire_fn) -> list:
    ids = expire_fn(lots, today)
    mapped = []
    by_id = {int(l["id"]): l for l in lots}
    for i in ids:
        row = by_id.get(int(i)) or {}
        if row.get("split_from"):
            mapped.append(int(row["split_from"]))
        else:
            mapped.append(int(i))
    return mapped

def eligibility_sql() -> str:
    return (
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0 "
        "AND data_quality='clean' AND split_from IS NULL"
    )
