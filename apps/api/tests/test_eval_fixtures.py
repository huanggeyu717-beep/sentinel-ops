"""evals/fixtures/inventory.json 与 dev seed 的一致性 (SPEC-007 第七节)。

那份快照是 grader 富化 zone 与数据集 lint 共用的**唯一**库存事实源, 两者都不连库;
一致性靠这条连库测试守着 —— dev seed 加了一个探头而快照没跟, 这里必须红。

断言是**逐字一致**不是"包含": 少一行多一行都算走散。seed_version 是快照内容的
sha256 前 16 位, 两边各自算、必须相等 —— 不引入第三个需要人工维护的版本号。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from test_agent_helpers import db

FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "inventory.json"


def _canonical_hash(data: dict) -> str:
    # seed_version 与 note (人读的截断说明) 都不进哈希 —— 哈希只盖数据本身
    body = {k: v for k, v in data.items() if k not in ("seed_version", "note")}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def test_inventory_fixture__matches_dev_seed_exactly(client):
    fixture = json.loads(FIXTURE.read_text())

    async def go(conn):
        zones = [
            {"id": r["id"], "name": r["name"]}
            for r in await conn.fetch("SELECT id, name FROM zones ORDER BY id")
        ]
        devices = [
            {"id": r["id"], "name": r["name"], "zone_id": r["zone_id"]}
            for r in await conn.fetch(
                "SELECT id, name, zone_id FROM devices ORDER BY id"
            )
        ]
        sensors = [
            {"id": r["id"], "zone_id": r["zone_id"], "device_id": r["device_id"]}
            for r in await conn.fetch(
                "SELECT id, zone_id, device_id FROM sensors ORDER BY id"
            )
        ]
        roles = [
            r["name"]
            for r in await conn.fetch(
                "SELECT DISTINCT r.name FROM roles r "
                "JOIN user_roles ur ON ur.role_id = r.id ORDER BY r.name"
            )
        ]
        return {"zones": zones, "devices": devices, "sensors": sensors,
                "roles_present": roles}

    actual = db(go)
    # 逐字一致, 不是"包含" —— 差一行就是快照走散
    assert fixture["zones"] == actual["zones"]
    assert fixture["devices"] == actual["devices"]
    assert fixture["sensors"] == actual["sensors"]
    assert fixture["roles_present"] == actual["roles_present"]
    # seed_version 两边各自算必须相等: 快照文件里的哈希要与 (a) 快照自身内容、
    # (b) 库里实际种子内容, 两个都对得上
    assert fixture["seed_version"] == _canonical_hash(fixture)
    assert fixture["seed_version"] == _canonical_hash(actual)
