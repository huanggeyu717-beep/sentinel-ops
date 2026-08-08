"""W3 第三段验收: 策略生命周期的 HTTP 层 (SPEC-006 第五节接口表 + 第七节主线 A/B)。

service 层的行为第二段已在 test_policy_service 验过; 本文件验的是壳:
- 两条主线全程走 HTTP, 用种子里的真实账号 (alex=operator, chris=manager A,
  dana=manager B, viewer@example.com=viewer);
- 每类 service 异常都有明确的状态码与人话响应体;
- 权限档位逐字对齐 SPEC-004 决策 6;
- 路由层权限门的存在性用话术钉住 (变异测试的红灯锚点, 见
  test_publish__operator_403_from_route_layer_gate 的注释)。
"""
from __future__ import annotations

import asyncio
import time

import asyncpg
import pytest
from conftest import DSN, insert_published_policy

from app.config import settings
from app.services import drill_service

GOOD_BODY = {
    "scope": {"type": "zone", "ids": [1, 2, 3]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}

BAD_ZONE_BODY = {**GOOD_BODY, "scope": {"type": "zone", "ids": [99]}}

CSV_SOURCE = "apps/device-sim/seed/waterlevel_readings.csv"


@pytest.fixture(scope="module")
def op_hdr(auth_headers):
    return auth_headers("alex@example.com")  # operator


@pytest.fixture(scope="module")
def mgr_a(auth_headers):
    return auth_headers("chris@example.com")  # manager A


@pytest.fixture(scope="module")
def mgr_b(auth_headers):
    return auth_headers("dana@example.com")  # manager B (迁移 0007 的种子)


@pytest.fixture(autouse=True)
def clean_drills():
    drill_service.reset()
    yield
    drill_service.reset()


def _fetchval(sql: str, *args):
    async def go():
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def _create(client, hdr, name, body):
    r = client.post("/policies", json={"name": name, "body": body}, headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


def _walk_to_awaiting(client, hdr, name, source="auto_close"):
    """create -> validate -> simulate -> request-approval, 返回 (created, approval_id)。"""
    created = _create(client, hdr, name, GOOD_BODY)
    vid = created["version_id"]
    r = client.post(f"/policy-versions/{vid}/validate", headers=hdr)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    r = client.post(f"/policy-versions/{vid}/simulate", json={"source": source}, headers=hdr)
    assert r.status_code == 200, r.text
    r = client.post(f"/policy-versions/{vid}/request-approval", headers=hdr)
    assert r.status_code == 200, r.text
    return created, r.json()["approval_id"]


def _wait_drill_terminal(client, hdr, drill_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/drills/{drill_id}", headers=hdr).json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"演练 {drill_id} 在 {timeout}s 内没进入终态")


# ===== 主线 A: operator 提交, manager 批准, 引擎接管 (SPEC-006 验收 1-9, 全程 HTTP) =====

def test_mainline_a__operator_submits_manager_approves_engine_takes_over(
    client, op_hdr, mgr_a, monkeypatch
):
    # 1. operator 写一条策略 (scope 指向不存在的 zone 99) -> 草稿建立
    created = _create(client, op_hdr, "wet-opens-http", BAD_ZONE_BODY)
    assert created["version"] == 1

    # 2. 静态校验拦住写错的引用 -> E_UNKNOWN_ZONE 且 hint 给出合法取值, 版本不转移
    r = client.post(f"/policy-versions/{created['version_id']}/validate", headers=op_hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    issue = next(i for i in body["issues"] if i["code"] == "E_UNKNOWN_ZONE")
    assert "99" in issue["message"] and issue["hint"]
    r = client.get(f"/policy-versions/{created['version_id']}", headers=op_hdr)
    assert r.json()["status"] == "draft"

    # 3. 改对 = 新建一版 (版本不可变), 校验通过 -> validated
    r = client.post(
        f"/policies/{created['policy_id']}/versions",
        json={"body": GOOD_BODY}, headers=op_hdr,
    )
    assert r.status_code == 201
    v2 = r.json()
    assert v2["version"] == 2
    r = client.post(f"/policy-versions/{v2['version_id']}/validate", headers=op_hdr)
    assert r.json() == {"ok": True, "status": "validated", "issues": []}

    # 4. 拿 344 条真实历史 CSV 回放 -> ReplayReport, 跑完即转 simulated。
    #    第二段在 service 层给事件补了 zone_id, SPEC-001 验收 8 那条
    #    "CSV 回放不覆盖开事故链路"的限制应已解除 —— 这里在 HTTP 层实测钉住。
    r = client.post(
        f"/policy-versions/{v2['version_id']}/simulate",
        json={"source": CSV_SOURCE}, headers=op_hdr,
    )
    assert r.status_code == 200, r.text
    report = r.json()
    # 344 条真实读数; events_count 更大是因为 CSV 装载器按仿真时间补心跳事件
    # (真实数据没有心跳报文, packages/scenario loader 的既定行为)
    assert report["events_count"] == 1258
    assert report["by_action_type"]["open_incident"] > 0, report["by_action_type"]
    assert report["skipped"] == []  # zone_id 补上后不再有 missing_context
    assert "side_effects_note" in report  # 不能让人误以为邮件真发出去了
    r = client.get(f"/policy-versions/{v2['version_id']}", headers=op_hdr)
    assert r.json()["status"] == "simulated"

    # 5. 提交审批 -> awaiting_approval + 一条待决审批
    r = client.post(f"/policy-versions/{v2['version_id']}/request-approval", headers=op_hdr)
    assert r.status_code == 200
    approval_id = r.json()["approval_id"]
    r = client.get(f"/policy-versions/{v2['version_id']}", headers=op_hdr)
    assert r.json()["status"] == "awaiting_approval"

    # 6. operator 尝试自己批 -> 403。证明的是 RBAC (operator 无 policies:approve),
    #    不是"禁止自批" —— 拦在路由层的权限门上, 话术是 require_permission 的
    r = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=op_hdr
    )
    assert r.status_code == 403
    assert "policies:approve" in r.json()["detail"]

    # 7. manager A 批准 -> decision=approved, 版本转 published
    r = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approved", "note": "回放分布正常"}, headers=mgr_a,
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "approved"
    assert r.json()["version_status"] == "published"

    # 8. manager A 发布 -> policy_publications 一行, approval_id 指向第 7 步那条
    r = client.post(f"/policy-versions/{v2['version_id']}/publish", headers=mgr_a)
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["approval_id"] == approval_id
    r = client.get(f"/policies/{created['policy_id']}", headers=op_hdr)
    publication = r.json()["publication"]
    assert publication is not None and publication["version"] == 2

    # 9. 跑 multi_sensor_escalation 场景, 验证策略确实接管了开事故。
    #    演练按仿真时间回放 (290s), 提速 100 倍压到 ~3s
    monkeypatch.setattr(settings(), "drill_speed", 100.0)
    r = client.post("/drills/multi_sensor_escalation", headers=op_hdr)
    assert r.status_code == 202, r.text
    final = _wait_drill_terminal(client, op_hdr, r.json()["drill_id"])
    assert final["status"] == "completed", final

    incidents = client.get("/incidents", headers=op_hdr).json()["incidents"]
    assert incidents, "场景里的湿事件应由已发布策略开出事故"
    runs = client.get(
        f"/policy-runs?policy_id={created['policy_id']}", headers=op_hdr
    ).json()["runs"]
    assert runs and all(run["policy_version_id"] == v2["version_id"] for run in runs)
    opened = [
        e for run in runs for e in run["effects"]
        if e["action_type"] == "open_incident" and e["outcome"] == "applied"
    ]
    assert opened, "policy_runs 里能查到是哪条策略哪一版开的"
    assert {e["incident_id"] for e in opened} <= {i["id"] for i in incidents}


# ===== 主线 B: manager 自己写的策略, 这条才验"禁止自批" (验收 10-12, 全程 HTTP) =====

def test_mainline_b__manager_self_approval_403_then_second_manager_approves(
    client, mgr_a, mgr_b
):
    # 10. manager A 写一条策略并提交审批 (manager 也有 operator 档的草稿能力)
    created, approval_id = _walk_to_awaiting(client, mgr_a, "mgr-own-http")

    # 11. manager A 自己批 -> 403 且审计留痕。他有审批权限 (RBAC 放行),
    #     被拦住的只能是自批规则 —— 话术必须是自批的人话, 不是 RBAC 的"当前角色无"
    r = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=mgr_a
    )
    assert r.status_code == 403
    assert "自己提交" in r.json()["detail"]
    assert "当前角色无" not in r.json()["detail"]
    audit = _fetchval(
        "SELECT count(*) FROM audit_log "
        "WHERE action = 'approval.self_approve_denied' AND entity_id = $1",
        str(approval_id),
    )
    assert audit == 1  # 独立事务写审计, 403 的回滚冲不掉留痕

    # 12. manager B 批准 -> 通过, 后续发布正常
    r = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=mgr_b
    )
    assert r.status_code == 200 and r.json()["decision"] == "approved"
    r = client.post(f"/policy-versions/{created['version_id']}/publish", headers=mgr_a)
    assert r.status_code == 200
    assert r.json()["approval_id"] == approval_id


# ===== 权限档位 (SPEC-004 决策 6 逐字对齐) =====

def test_policies_read__viewer_200_write_403(client, viewer_headers):
    assert client.get("/policies", headers=viewer_headers).status_code == 200
    assert client.get("/policy-runs", headers=viewer_headers).status_code == 200
    r = client.post(
        "/policies", json={"name": "viewer-try", "body": GOOD_BODY}, headers=viewer_headers
    )
    assert r.status_code == 403  # 写草稿是 operator+
    r = client.post("/policies/1/revoke", headers=viewer_headers)
    assert r.status_code == 403  # 撤销是 manager+


def test_policies__unauthenticated_401(client):
    assert client.get("/policies").status_code == 401
    assert client.post(
        "/policies", json={"name": "anon", "body": GOOD_BODY}
    ).status_code == 401


def test_publish__operator_403_from_route_layer_gate(client, op_hdr):
    """变异测试的红灯锚点: 断言的是**路由层** require_permission 的话术。

    权限有三道防线: 路由层门 (最外层快速失败) / service 层闸 / 数据库外键兜底。
    若把 publish 路由上的 manager 门去掉, service 层仍会 403, 状态码测不出区别 ——
    但话术会从"当前角色无 ... 权限"变成 service 映射的"该操作需要 manager 及以上角色",
    这条断言就红了。交付报告里的变异测试跑的就是这一条。
    """
    r = client.post("/policy-versions/1/publish", headers=op_hdr)
    assert r.status_code == 403
    assert "当前角色无 policies:approve 权限" in r.json()["detail"]


def test_revoke__operator_403(client, op_hdr):
    assert client.post("/policies/1/revoke", headers=op_hdr).status_code == 403


# ===== 异常 -> 状态码: 每类一条 (SPEC-006 第五节 + ADR-007) =====

def test_not_found__policy_version_approval_source_all_404(client, op_hdr, mgr_a):
    assert client.get("/policies/999999", headers=op_hdr).status_code == 404
    assert client.get("/policy-versions/999999", headers=op_hdr).status_code == 404
    assert client.post(
        "/policy-versions/999999/validate", headers=op_hdr
    ).status_code == 404
    r = client.post(
        "/approvals/999999/decide", json={"decision": "approved"}, headers=mgr_a
    )
    assert r.status_code == 404

    created = _create(client, op_hdr, "src-404", GOOD_BODY)
    client.post(f"/policy-versions/{created['version_id']}/validate", headers=op_hdr)
    for source in ("no_such_scenario", "../outside.yaml", "/etc/passwd"):
        r = client.post(
            f"/policy-versions/{created['version_id']}/simulate",
            json={"source": source}, headers=op_hdr,
        )
        assert r.status_code == 404, (source, r.text)


def test_validate__wrong_status_409_with_current_status(client, op_hdr):
    created = _create(client, op_hdr, "double-validate", GOOD_BODY)
    vid = created["version_id"]
    assert client.post(f"/policy-versions/{vid}/validate", headers=op_hdr).json()["ok"]
    r = client.post(f"/policy-versions/{vid}/validate", headers=op_hdr)  # 已 validated
    assert r.status_code == 409
    assert "validated" in r.json()["detail"]  # 人话里带当前状态


def test_request_approval__from_draft_409(client, op_hdr):
    created = _create(client, op_hdr, "skip-steps", GOOD_BODY)
    r = client.post(
        f"/policy-versions/{created['version_id']}/request-approval", headers=op_hdr
    )
    assert r.status_code == 409  # draft 不能直接提交审批, 要先校验+模拟


def test_decide__already_decided_409(client, op_hdr, mgr_a, mgr_b):
    _, approval_id = _walk_to_awaiting(client, op_hdr, "decide-twice")
    r = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=mgr_a
    )
    assert r.status_code == 200
    r = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "rejected"}, headers=mgr_b
    )
    assert r.status_code == 409
    assert "已有结论" in r.json()["detail"]


def test_publish__without_approved_status_409(client, op_hdr, mgr_a):
    created = _create(client, op_hdr, "publish-draft", GOOD_BODY)
    r = client.post(f"/policy-versions/{created['version_id']}/publish", headers=mgr_a)
    assert r.status_code == 409
    assert "draft" in r.json()["detail"]  # 当前状态说给人听


def test_revoke__nothing_active_409(client, op_hdr, mgr_a):
    created = _create(client, op_hdr, "revoke-nothing", GOOD_BODY)
    r = client.post(f"/policies/{created['policy_id']}/revoke", headers=mgr_a)
    assert r.status_code == 409


def test_create__body_outside_whitelist_422(client, op_hdr):
    """白名单以外的动作在请求体 Schema 层就说不出来 (Policy 模型 extra=forbid,
    与 policy_json_schema 同一来源), 连草稿都不建。"""
    bad = {**GOOD_BODY, "actions": [{"type": "delete_database"}]}
    r = client.post("/policies", json={"name": "evil", "body": bad}, headers=op_hdr)
    assert r.status_code == 422
    assert client.get("/policies", headers=op_hdr).json()["policies"] == []


def test_publish__cross_policy_conflict_422_names_the_other_policy(client, mgr_a):
    """PublishRejected -> 422, conflicts 指出与哪条已发布策略冲突。"""
    import json as _json

    def led(state):
        return {
            "scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "sensor_state_changed", "to": "WET"},
            "conditions": [],
            "actions": [
                {"type": "set_led", "target": "incident_device", "state": state}
            ],
            "cooldown_s": 60,
        }

    asyncio.run(insert_published_policy("led-on-http", _json.dumps(led("ON"))))
    candidate = asyncio.run(
        insert_published_policy("led-off-http", _json.dumps(led("OFF")))
    )
    r = client.post(f"/policies/{candidate['policy_id']}/revoke", headers=mgr_a)
    assert r.status_code == 200
    r = client.post(
        f"/policy-versions/{candidate['version_id']}/publish", headers=mgr_a
    )
    assert r.status_code == 422, r.text
    conflicts = r.json()["detail"]["conflicts"]
    assert conflicts[0]["code"] == "E_ACTION_MUTEX"
    assert conflicts[0]["policy_name"] == "led-on-http"


# ===== 列表形状与 /docs 可交互 =====

def test_list_policies__shows_active_and_latest_version(client, op_hdr, published_baseline):
    r = client.get("/policies", headers=op_hdr)
    row = next(
        p for p in r.json()["policies"] if p["id"] == published_baseline["policy_id"]
    )
    assert row["active_version"] == 1 and row["latest_version"] == 1
    assert row["publication_id"] == published_baseline["publication_id"]


def test_openapi__body_schema_same_source_as_policy_json_schema(client):
    """/docs 可交互调用 (W1 验收项) 且 DSL Schema 与 policy_json_schema() 同源:
    请求体里的 body 就是 policy_engine.Policy 模型, 不另生成一套。"""
    from policy_engine import policy_json_schema

    spec = client.get("/openapi.json").json()
    for path in (
        "/policies", "/policy-versions/{version_id}/simulate",
        "/approvals/{approval_id}/decide", "/policy-runs", "/employees",
    ):
        assert path in spec["paths"], path
    # 所有 POST 都有显式请求体或路径参数; simulate/decide/create 的请求体是命名模型
    assert "requestBody" in spec["paths"]["/policies"]["post"]
    # Policy 组件与 policy_json_schema() 的属性集一致 (同一个 pydantic 模型生成)
    components = spec["components"]["schemas"]
    policy_schema = components.get("Policy") or components.get("Policy-Input")
    assert policy_schema is not None
    assert set(policy_schema["properties"]) == set(policy_json_schema()["properties"])
