"""拆袋后的列表标注与下架目标：每一行都是真实批次。

在架拆袋确认后，母批（余量已减）与子批在架并存，各自是独立的在架行。
这里不做任何"身份回映射"：总表行数、过期下架目标、扣减候选都指向行自身的 id；
履历里也不会把一次分装回放成又一次扣减。
"""

def annotate_fridge(rows: list) -> list:
    """每行即真实批次：hit_id / shown_id 就是行自身的 id。

    子批（split_from 非空）不再把命中目标指到母批——收走列谁、回包打谁，
    都落在留下的那批本人身上。
    """
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
    """过期下架打真实行：子批到期下子批本人，不回映射到母批身份。"""
    return [int(i) for i in expire_fn(lots, today)]
