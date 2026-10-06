"""拆袋（在架分装）一致性互证。

每条测试对应一条不变量：

- 预览不改数字；确认后母行与子行并存且守恒（母余量 + 子批 == 拆前）
- 总表与分层页同一收口：两页行数/合计一致，失败时两页都不多行
- 先到期打到拆出来的行：子批与母批同资格进 FEFO 候选
- 回包（过期下架）与收走（扣减候选）都指向留下的真实行，不打母批身份
- 拆袋回包不含扣减形状：履历不会按子行再扣一遍
- 脏行/负余量行：能不能拆与能不能进扣减候选是同一套资格
- 拆量为零或大于余量：整单失败，不留痕迹
- 拆袋确认与扣减叠在同一母行：要么都成，要么都回滚，不许一半
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import connect


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def lots_table():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM lots ORDER BY id")]
    c.close()
    return rows


def fridge_rows(client, layer=None):
    q = "/api/fridge" + (f"?layer={layer}" if layer else "")
    r = client.get(q)
    assert r.status_code == 200
    return r.json()


def shelf_total(rows):
    return sum(float(r["qty_remain"]) for r in rows)


def consumptions_rows():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM consumptions")]
    c.close()
    return rows


# 种子：lot1 牛奶2盒 2026-10-01 / lot2 牛奶1盒 2026-09-28 / lot3 鸡蛋12个 2026-11-01
#       lot4 冻饺1袋 dirty / lot5 鸡蛋-3 dirty(负余量)


def test_preview_does_not_change_numbers(client):
    before_db = lots_table()
    before_fridge = fridge_rows(client)
    r = client.post("/api/splits/preview", json={"lot_id": 3, "qty": 5})
    assert r.status_code == 200
    p = r.json()
    assert p["before"] == 12 and p["parent_after"] == 7 and p["child_qty"] == 5
    assert p["sum_after"] == 12 and p["conserved"] is True
    assert lots_table() == before_db, "预览不得写库"
    assert fridge_rows(client) == before_fridge, "预览不得改总表"


def test_confirm_conservation_parent_child_coexist(client):
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 5})
    assert r.status_code == 200
    p = r.json()
    assert p["parent_id"] == 3
    child_id = p["child_id"]
    assert p["before"] == 12 and p["parent_after"] == 7 and p["child_qty"] == 5
    assert p["sum_after"] == 12 and p["conserved"] is True

    lots = {l["id"]: l for l in lots_table()}
    assert lots[3]["qty_remain"] == 7 and lots[3]["status"] == "on_shelf"
    child = lots[child_id]
    assert child["qty_in"] == 5 and child["qty_remain"] == 5
    assert child["split_from"] == 3 and child["status"] == "on_shelf"
    assert child["expiry"] == lots[3]["expiry"], "子批与母批同到期"

    # 守恒：拆后总量 == 拆前总量（13 = 2+1+12+1-3）
    assert shelf_total(fridge_rows(client)) == pytest.approx(13)


def test_split_reply_carries_no_deduction_shape(client):
    """拆袋是移动不是扣减：回包不得带 deductions/假身份，履历不得多一条扣减。"""
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 5})
    assert r.status_code == 200
    p = r.json()
    for key in ("deductions", "consume_lot_id", "history_lot_id"):
        assert key not in p, f"拆袋回包不得含 {key}（履历会按它再扣一遍）"
    assert consumptions_rows() == [], "分装不得写入消费履历"


def test_fridge_and_layer_pages_same_closure(client):
    all_before = fridge_rows(client)
    mid_before = fridge_rows(client, "mid")
    upper_before = fridge_rows(client, "upper")

    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 5})
    assert r.status_code == 200

    all_after = fridge_rows(client)
    mid_after = fridge_rows(client, "mid")
    # 两页同一收口：总表多一行，该分层页也多同一行，两页合计都等于拆前
    assert len(all_after) == len(all_before) + 1
    assert len(mid_after) == len(mid_before) + 1
    assert len(fridge_rows(client, "upper")) == len(upper_before)
    assert shelf_total(all_after) == pytest.approx(shelf_total(all_before))
    assert shelf_total(mid_after) == pytest.approx(shelf_total(mid_before))
    # 分层页的行就是总表的行，不存在一页多一行
    assert {r["id"] for r in mid_after} <= {r["id"] for r in all_after}


def test_fefo_hits_split_child(client):
    """先到期应打到拆出来的行：子批是真实候选，不是母批身份的附属。"""
    r = client.post("/api/splits/confirm", json={"lot_id": 1, "qty": 1})
    assert r.status_code == 200
    child_id = r.json()["child_id"]

    # 牛奶 FEFO：lot2(09-28) → lot1(10-01) → 子批(10-01)，消 2.5 必然落到子批
    r = client.post("/api/consume", json={"item_id": 1, "qty": 2.5})
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    hit_ids = [d["lot_id"] for d in result["deductions"]]
    assert child_id in hit_ids, "扣减必须能打到拆出来的子批"
    lots = {l["id"]: l for l in lots_table()}
    assert lots[child_id]["qty_remain"] == pytest.approx(0.5)


def test_full_split_then_consume_hits_child(client):
    """全拆（拆量==余量）后母批余量为 0，量必须仍可由子批被收走。"""
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 12})
    assert r.status_code == 200
    child_id = r.json()["child_id"]

    r = client.post("/api/consume", json={"item_id": 2, "qty": 12})
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    assert [d["lot_id"] for d in result["deductions"]] == [child_id]


def test_dirty_and_negative_share_one_eligibility_rule(client):
    """脏行/负余量行：不能拆，也不能进扣减候选 —— 同一套资格。"""
    for bad_lot in (4, 5):
        r = client.post("/api/splits/confirm", json={"lot_id": bad_lot, "qty": 0.5})
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "lot_not_eligible"

    # 冻饺只有脏批：收不走
    r = client.post("/api/consume", json={"item_id": 3, "qty": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "short"

    # 鸡蛋 12 个干净 + (-3) 脏：能收走 12，多 0.5 都不行（脏行不算候选）
    r = client.post("/api/consume", json={"item_id": 2, "qty": 12})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/api/consume", json={"item_id": 2, "qty": 0.5})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "short"


def test_split_zero_or_exceed_fails_whole_order(client):
    before = lots_table()
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 0})
    assert r.status_code == 400
    assert r.json()["detail"] == "split_qty_non_positive"
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": -2})
    assert r.status_code == 400
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 13})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "split_exceeds_remain"
    assert lots_table() == before, "整单失败不得留下任何痕迹"


def test_failed_split_leaves_no_extra_rows_on_either_page(client):
    before_all = fridge_rows(client)
    before_mid = fridge_rows(client, "mid")
    client.post("/api/splits/confirm", json={"lot_id": 4, "qty": 0.5})   # 脏批
    client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 99})    # 超量
    client.post("/api/splits/confirm", json={"lot_id": 9999, "qty": 1})  # 不存在
    assert fridge_rows(client) == before_all, "总表不得多出行"
    assert fridge_rows(client, "mid") == before_mid, "分层页不得多出行"


def test_split_atomic_rollback_when_child_insert_fails(client, monkeypatch):
    """子批插入失败：母批扣减必须回滚 —— 不许母行已减而子行没有。"""
    import app.main as main_mod

    class FlakyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, args=()):
            if "INSERT INTO lots" in sql:
                raise sqlite3.OperationalError("boom")
            return self._real.execute(sql, args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = main_mod.connect
    monkeypatch.setattr(main_mod, "connect", lambda: FlakyConn(real_connect()))

    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 5})
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "connect", real_connect)

    lots = {l["id"]: l for l in lots_table()}
    assert lots[3]["qty_remain"] == 12, "母批扣减必须随事务回滚"
    assert len(lots_table()) == 5, "不得留下孤儿子行"
    assert len(fridge_rows(client)) == 5


def test_consume_rolls_back_when_lot_changed(client, monkeypatch):
    """扣减叠在同一母行：计划与落库之间行变了，整单回滚，不许扣一半。"""
    import app.main as main_mod

    fake_plan = {
        "ok": True, "reason": "", "short": 0.0,
        "deductions": [
            {"lot_id": 3, "take": 5, "expiry": "2026-11-01"},
            {"lot_id": 99999, "take": 1, "expiry": None},
        ],
    }
    monkeypatch.setattr(main_mod, "consume_fefo", lambda lots, qty: fake_plan)

    r = client.post("/api/consume", json={"item_id": 2, "qty": 6})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "lot_changed"

    lots = {l["id"]: l for l in lots_table()}
    assert lots[3]["qty_remain"] == 12, "第一笔扣减必须随整单回滚"
    assert consumptions_rows() == [], "失败单不得写入履历"


def test_expire_sweep_hits_real_rows_not_parent_identity(client):
    """回包打谁：子批到期下子批本人，不回映射到母批身份。"""
    r = client.post("/api/lots", json={"item_id": 1, "qty": 4, "expiry": "2020-01-01"})
    parent_id = r.json()["id"]
    r = client.post("/api/splits/confirm", json={"lot_id": parent_id, "qty": 1})
    assert r.status_code == 200
    child_id = r.json()["child_id"]

    r = client.post("/api/expire-sweep")
    assert r.status_code == 200
    expired = set(r.json()["expired_ids"])
    assert parent_id in expired and child_id in expired

    lots = {l["id"]: l for l in lots_table()}
    assert lots[parent_id]["status"] == "expired"
    assert lots[child_id]["status"] == "expired", "子批必须本人下架"


def test_fridge_annotations_point_at_real_rows(client):
    """收走列谁：总表每一行的命中目标就是行自身，子批不指母批。"""
    r = client.post("/api/splits/confirm", json={"lot_id": 3, "qty": 5})
    child_id = r.json()["child_id"]
    rows = fridge_rows(client)
    for row in rows:
        assert row["hit_id"] == row["id"]
        assert row["shown_id"] == row["id"]
    child_row = next(r for r in rows if r["id"] == child_id)
    assert child_row["hit_id"] == child_id
