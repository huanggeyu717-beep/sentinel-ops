"""GET /employees —— W2 遗留项 (SPEC-006 第五节): 派单下拉框的数据源。

权限与 /incidents 同档 (viewer+); 响应不含 rfid_uid (刷卡凭据不进名录)。
"""
from __future__ import annotations


def test_list_employees__viewer_ok_with_zone_names(client, viewer_headers):
    r = client.get("/employees", headers=viewer_headers)
    assert r.status_code == 200, r.text
    employees = {e["name"]: e for e in r.json()["employees"]}
    # 种子里的三名员工都在 (含没有登录账号的 Bo Wang —— 名录按 employees 表出,
    # 与 users 无关, 派单派的是现场员工不是登录账号)
    assert {"Alex Chen", "Bo Wang", "Chris Li"} <= set(employees)
    alex = employees["Alex Chen"]
    assert alex["zone_id"] == 1 and alex["zone_name"] == "Zone 1 - 生鲜区"
    assert alex["role"] == "operator"
    assert "rfid_uid" not in alex  # 刷卡凭据不进响应面


def test_list_employees__unauthenticated_401(client):
    assert client.get("/employees").status_code == 401
