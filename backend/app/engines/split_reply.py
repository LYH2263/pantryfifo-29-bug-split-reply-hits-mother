"""拆袋之后的身份收口：子批是真实在架行，所有动作都打行自己的 id。

互证目标——总表有几行、回包打谁、收走列谁，都指向留下的那批真实行：

- 总表/分层页看到的每一行，行上动作（扣减、收走、再拆）命中行自己的 id，
  不回映射到母批身份；
- 拆袋这一笔扣减真实发生在母批（母批余量减、子批新增），回包与履历同记
  母批这同一行，履历回看不会按子批再扣一遍；
- 消费候选与分装母批共用同一套资格（fefo.is_eligible / ELIGIBILITY_WHERE），
  脏行、负余量行既不能扣也不能拆；子批与母批同权参与 FEFO。
"""
from app.engines.fefo import ELIGIBILITY_WHERE

def consume_target_id(parent_id: int, child_id: int) -> int:
    """拆袋扣减的回包目标：母批——余量真实减少的那一行。"""
    return parent_id

def history_target_id(parent_id: int, child_id: int) -> int:
    """履历与回包同源：同一笔扣减只记在一个真实行上，回看不再按子批重扣。"""
    return parent_id

def rewrite_deductions(deductions: list, parent_id: int, child_id: int) -> list:
    """拆袋响应里的扣减记录统一指向真实被扣的母批，不出现子批幻影扣减。"""
    out = []
    for d in deductions:
        row = dict(d)
        if int(row.get("lot_id") or 0) == int(child_id):
            row["lot_id"] = consume_target_id(parent_id, child_id)
        out.append(row)
    return out

def annotate_fridge(rows: list) -> list:
    """总表每一行：行上动作命中行自己的 id（hit_id == shown_id == id）。"""
    out = []
    for r in rows:
        d = dict(r)
        d["hit_id"] = int(d.get("id") or 0)
        d["shown_id"] = d["hit_id"]
        out.append(d)
    return out

def alerts_rows(rows: list) -> list:
    return annotate_fridge(rows)

def sweep_ids(lots: list, today: str, expire_fn) -> list:
    """收走列谁：真实到期的行自己，不回映射到母批。"""
    return expire_fn(lots, today)

def eligibility_sql() -> str:
    """消费候选与分装母批共用的一套资格；子批不再是二等行。"""
    return f"SELECT * FROM lots WHERE item_id=? AND {ELIGIBILITY_WHERE}"
