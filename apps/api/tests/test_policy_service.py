"""W3 验收: 策略生命周期 (草稿/校验/模拟/审批/发布/撤销)。对应 docs/specs/SPEC-006。

第三段才建 HTTP 路由, 本文件按 SPEC-006 的要求**直接调 service** 验收
(验收 10-12 明确要求不经过 HTTP)。每个用例在独立事件循环 + 独立 NullPool 引擎里跑,
不与 TestClient 的应用事件循环共享连接。

角色: alex (user 3, operator) 提交; chris (user 2, manager A); dana (种子新增的
manager B, id 由迁移里的序列分配, 用邮箱查)。
"""
from __future__ import annotations

import json

import pytest
from conftest import insert_published_policy
from sqlalchemy import text

from app.services import ingest_service, policy_service
from app.services.auth_service import AuthUser

TS = 1_773_600_000_000

GOOD_BODY = {
    "scope": {"type": "zone", "ids": [1, 2, 3]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}

BAD_ZONE_BODY = {**GOOD_BODY, "scope": {"type": "zone", "ids": [99]}}

ALEX = AuthUser(id=3, email="alex@example.com", display_name="Alex Chen",
                employee_id=1, roles=["operator"])
CHRIS = AuthUser(id=2, email="chris@example.com", display_name="Chris Li",
                 employee_id=3, roles=["manager"])


async def _dana(factory) -> AuthUser:
    """种子里的第二个 manager。id 由序列分配 (迁移 0007), 只能按邮箱查。"""
    async with factory() as s:
        row = (await s.execute(
            text("SELECT id FROM users WHERE email = 'dana@example.com'")
        )).scalar_one()
    return AuthUser(id=row, email="dana@example.com", display_name="Dana Park",
                    employee_id=None, roles=["manager"])


async def _version_status(factory, version_id: int) -> str:
    async with factory() as s:
        return (await s.execute(
            text("SELECT status FROM policy_versions WHERE id = :id"), {"id": version_id}
        )).scalar_one()


# ===== 主线 A: operator 提交, manager 批准 (验收 1-9) =====

def test_mainline_a__operator_submits_manager_approves_engine_takes_over(svc):
    async def go(factory):
        # 1. operator 写一条策略 -> 草稿建立
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "wet-opens", BAD_ZONE_BODY, ALEX.id)
        assert created["version"] == 1

        # 2. 静态校验拦住写错的引用: zone 99 -> E_UNKNOWN_ZONE 且 hint 给出合法取值
        async with factory() as s, s.begin():
            res = await policy_service.validate_version(s, created["version_id"])
        assert res["ok"] is False
        issue = next(i for i in res["issues"] if i["code"] == "E_UNKNOWN_ZONE")
        assert "99" in issue["message"] and "1" in issue["hint"]
        assert await _version_status(factory, created["version_id"]) == "draft"  # 不转移

        # 3. 改对 = 新建一版 (版本不可变), 校验通过 -> validated
        async with factory() as s, s.begin():
            v2 = await policy_service.add_version(s, created["policy_id"], GOOD_BODY, ALEX.id)
        async with factory() as s, s.begin():
            res2 = await policy_service.validate_version(s, v2["version_id"])
        assert res2 == {"ok": True, "status": "validated", "issues": []}

        # 4. 拿 344 条真实历史数据回放 -> ReplayReport, 跑完即转 simulated
        async with factory() as s, s.begin():
            report = await policy_service.simulate_version(
                s, v2["version_id"], "apps/device-sim/seed/waterlevel_readings.csv"
            )
        assert report["by_action_type"]["open_incident"] > 0
        assert report["tick_seconds"] == 3600  # 与线上 SENTINEL_ENGINE_TICK_SECONDS 一致
        assert await _version_status(factory, v2["version_id"]) == "simulated"

        # 5. 提交审批 -> awaiting_approval + 一条 decision IS NULL 的记录
        async with factory() as s, s.begin():
            req = await policy_service.request_approval(s, v2["version_id"], ALEX.id)
        assert await _version_status(factory, v2["version_id"]) == "awaiting_approval"
        async with factory() as s:
            decision = (await s.execute(
                text("SELECT decision FROM approvals WHERE id = :id"),
                {"id": req["approval_id"]},
            )).scalar_one_or_none()
        assert decision is None

        # 6. operator 尝试自己批 -> 403。这一步证明的是 RBAC (operator 无权审批),
        #    不是"禁止自批" —— 判断次序刻意是权限在前 (SPEC-006 验收 6)
        with pytest.raises(policy_service.PermissionDenied):
            async with factory() as s, s.begin():
                await policy_service.decide_approval(
                    s, req["approval_id"], ALEX, "approved", audit_factory=factory
                )

        # 7. manager A 批准 -> decision='approved', 版本转 published
        async with factory() as s, s.begin():
            decided = await policy_service.decide_approval(
                s, req["approval_id"], CHRIS, "approved", audit_factory=factory
            )
        assert decided["decision"] == "approved"
        assert await _version_status(factory, v2["version_id"]) == "published"

        # 8. manager A 发布 -> policy_publications 出现一行, approval_id 指向第 7 步那条
        async with factory() as s, s.begin():
            pub = await policy_service.publish_version(s, v2["version_id"], CHRIS)
        assert pub["approval_id"] == req["approval_id"]
        async with factory() as s:
            row = (await s.execute(
                text("SELECT approval_id, revoked_at FROM policy_publications "
                     "WHERE id = :id"), {"id": pub["publication_id"]},
            )).mappings().one()
        assert row["approval_id"] == req["approval_id"] and row["revoked_at"] is None

        # 9. 新策略确实接管了开事故: 湿事件进来, 事故由这条策略开出,
        #    policy_runs 里能查到是哪条策略哪一版 (完整场景包回放另见 test_policy_runtime)
        async with factory() as s, s.begin():
            r = await ingest_service.ingest_event(s, {
                "kind": "sensor_state", "device_id": "Arduino1", "ts": TS,
                "sensor_id": 1, "state": "WET", "value": 845,
            })
        assert r.incident_id is not None
        async with factory() as s:
            run = (await s.execute(
                text("SELECT policy_id, policy_version_id, effects FROM policy_runs")
            )).mappings().one()
        assert run["policy_id"] == created["policy_id"]
        assert run["policy_version_id"] == v2["version_id"]
        effects = run["effects"] if isinstance(run["effects"], list) \
            else json.loads(run["effects"])
        assert effects[0]["action_type"] == "open_incident"
        assert effects[0]["incident_id"] == r.incident_id
        # 开单的 actor 口径: policy:{id}@v{version} (SPEC-006 对 SPEC-003 的修订 1)
        async with factory() as s:
            actor = (await s.execute(
                text("SELECT actor FROM incident_events WHERE kind = 'opened'")
            )).scalar_one()
        assert actor == f"policy:{created['policy_id']}@v2"

    svc(go)


# ===== 主线 B: manager 自己写的策略, 这条才验"禁止自批" (验收 10-12) =====

def test_mainline_b__manager_self_approval_blocked_second_manager_passes(svc):
    async def go(factory):
        dana = await _dana(factory)
        # 10. manager A 写一条策略并提交审批
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "mgr-own", GOOD_BODY, CHRIS.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, created["version_id"])
        async with factory() as s, s.begin():
            await policy_service.simulate_version(s, created["version_id"], "auto_close")
        async with factory() as s, s.begin():
            req = await policy_service.request_approval(s, created["version_id"], CHRIS.id)

        # 11. manager A 自己批 -> 403 且审计留痕。他有审批权限 (RBAC 放行),
        #     被拦住的原因只能是自批规则 —— 不变量 1 那一半的真实证明
        with pytest.raises(policy_service.SelfApprovalDenied):
            async with factory() as s, s.begin():
                await policy_service.decide_approval(
                    s, req["approval_id"], CHRIS, "approved", audit_factory=factory
                )
        # 审计走独立事务: 403 引发的回滚冲不掉留痕
        async with factory() as s:
            audit = (await s.execute(
                text("SELECT count(*) FROM audit_log "
                     "WHERE action = 'approval.self_approve_denied' AND entity_id = :id"),
                {"id": str(req["approval_id"])},
            )).scalar_one()
        assert audit == 1
        # 审批仍是待决的
        assert await _version_status(factory, created["version_id"]) == "awaiting_approval"

        # 12. manager B 批准 -> 通过, 后续发布正常
        async with factory() as s, s.begin():
            decided = await policy_service.decide_approval(
                s, req["approval_id"], dana, "approved", audit_factory=factory
            )
        assert decided["decision"] == "approved"
        async with factory() as s, s.begin():
            pub = await policy_service.publish_version(s, created["version_id"], CHRIS)
        assert pub["approval_id"] == req["approval_id"]

    svc(go)


def test_decide__operator_rbac_denied_before_self_check(svc):
    """operator 自己提交自己批: 先吃 RBAC 403, 而不是自批 403 —— 两个错误不能混。"""
    async def go(factory):
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "rbac-order", GOOD_BODY, ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, created["version_id"])
        async with factory() as s, s.begin():
            await policy_service.simulate_version(s, created["version_id"], "auto_close")
        async with factory() as s, s.begin():
            req = await policy_service.request_approval(s, created["version_id"], ALEX.id)
        with pytest.raises(policy_service.PermissionDenied):
            async with factory() as s, s.begin():
                await policy_service.decide_approval(
                    s, req["approval_id"], ALEX, "approved", audit_factory=factory
                )

    svc(go)


# ===== 发布与撤销的边界 =====

def test_publish__without_approved_status_conflicts(svc):
    """跳过审批直接发布: service 层 409; 数据库层的物理拦截见 test_policy_constraints。"""
    async def go(factory):
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "no-approve", GOOD_BODY, ALEX.id)
        with pytest.raises(policy_service.NotApproved):
            async with factory() as s, s.begin():
                await policy_service.publish_version(s, created["version_id"], CHRIS)

    svc(go)


def test_publish__operator_rbac_denied(svc):
    async def go(factory):
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "op-publish", GOOD_BODY, ALEX.id)
        with pytest.raises(policy_service.PermissionDenied):
            async with factory() as s, s.begin():
                await policy_service.publish_version(s, created["version_id"], ALEX)

    svc(go)


def test_publish__second_active_version_conflicts(svc):
    """同一策略已有生效版本: 先撤销再发布 (partial unique index 的应用层预检)。"""
    async def go(factory):
        seeded = await insert_published_policy("dup-active", json.dumps(GOOD_BODY))
        # 同一策略再走一版到 published, 然后发布 -> 409
        async with factory() as s, s.begin():
            v2 = await policy_service.add_version(s, seeded["policy_id"], GOOD_BODY, ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, v2["version_id"])
        async with factory() as s, s.begin():
            await policy_service.simulate_version(s, v2["version_id"], "auto_close")
        async with factory() as s, s.begin():
            req = await policy_service.request_approval(s, v2["version_id"], ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.decide_approval(
                s, req["approval_id"], CHRIS, "approved", audit_factory=factory
            )
        with pytest.raises(policy_service.AlreadyPublished):
            async with factory() as s, s.begin():
                await policy_service.publish_version(s, v2["version_id"], CHRIS)

    svc(go)


def test_rollback__republish_reuses_original_approval_without_new_row(svc):
    """验收 22: 回滚 = 撤销 + 重新发布旧版本, 复用当年那条审批, 不产生新 approvals 行。"""
    async def go(factory):
        v1 = await insert_published_policy("rollback-me", json.dumps(GOOD_BODY))
        # v2 顶掉 v1: 撤销 v1 的发布, v2 走完整流程上线
        async with factory() as s, s.begin():
            await policy_service.revoke_publication(s, v1["policy_id"], CHRIS)
        async with factory() as s, s.begin():
            v2 = await policy_service.add_version(s, v1["policy_id"], GOOD_BODY, ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, v2["version_id"])
        async with factory() as s, s.begin():
            await policy_service.simulate_version(s, v2["version_id"], "auto_close")
        async with factory() as s, s.begin():
            req2 = await policy_service.request_approval(s, v2["version_id"], ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.decide_approval(
                s, req2["approval_id"], CHRIS, "approved", audit_factory=factory
            )
        async with factory() as s, s.begin():
            await policy_service.publish_version(s, v2["version_id"], CHRIS)

        async with factory() as s:
            before = (await s.execute(text("SELECT count(*) FROM approvals"))).scalar_one()

        # 回滚到 v1: 撤销 v2 + 重新发布 v1
        async with factory() as s, s.begin():
            await policy_service.revoke_publication(s, v1["policy_id"], CHRIS)
        async with factory() as s, s.begin():
            pub = await policy_service.publish_version(s, v1["version_id"], CHRIS)

        assert pub["approval_id"] == v1["approval_id"]  # 复用当年那条审批
        async with factory() as s:
            after = (await s.execute(text("SELECT count(*) FROM approvals"))).scalar_one()
        assert after == before  # 没有新的 approvals 行

    svc(go)


def test_revoke__nothing_active_conflicts(svc):
    async def go(factory):
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "no-pub", GOOD_BODY, ALEX.id)
        with pytest.raises(policy_service.NothingToRevoke):
            async with factory() as s, s.begin():
                await policy_service.revoke_publication(s, created["policy_id"], CHRIS)

    svc(go)


# ===== 发布前的跨策略检查 (SPEC-006 第五节: 拒绝而不是警告) =====

def _led_body(state: str) -> dict:
    return {
        "scope": {"type": "zone", "ids": [1]},
        "trigger": {"type": "sensor_state_changed", "to": "WET"},
        "conditions": [],
        "actions": [{"type": "set_led", "target": "incident_device", "state": state}],
        "cooldown_s": 60,
    }


def test_publish__opposite_set_led_in_same_scope_rejected(svc):
    """同一触发时机对同一对象一条点灯一条灭灯: 拒绝发布并指出是哪条策略。"""
    async def go(factory):
        await insert_published_policy("led-on", json.dumps(_led_body("ON")))
        candidate = await insert_published_policy("led-off", json.dumps(_led_body("OFF")))
        # candidate 的发布记录先撤掉, 再经 service 发布 -> 触发跨策略检查
        async with factory() as s, s.begin():
            await policy_service.revoke_publication(s, candidate["policy_id"], CHRIS)
        with pytest.raises(policy_service.PublishRejected) as e:
            async with factory() as s, s.begin():
                await policy_service.publish_version(s, candidate["version_id"], CHRIS)
        conflict = e.value.conflicts[0]
        assert conflict["code"] == "E_ACTION_MUTEX"
        assert conflict["policy_name"] == "led-on"  # 指出是哪条策略

    svc(go)


def test_publish__cross_policy_trigger_loop_rejected(svc):
    """A 的动作能唤醒 B、B 的动作能唤醒 A -> 拒绝发布。

    环里的 B (incident_elapsed + open_incident) 单策略静态检查本会拦住,
    这里用 SQL 直插已发布状态构造"已经在线"的极端情况 —— 发布前检查是第二道防线。
    """
    async def go(factory):
        loop_body = {
            "scope": {"type": "zone", "ids": [1]},
            "trigger": {"type": "incident_elapsed", "in_status": "open", "for_s": 120},
            "conditions": [],
            "actions": [{"type": "open_incident", "severity": "normal"}],
            "cooldown_s": 60,
        }
        await insert_published_policy("loop-b", json.dumps(loop_body))
        candidate = await insert_published_policy("loop-c", json.dumps({
            **loop_body, "trigger": {"type": "incident_elapsed", "in_status": "open",
                                     "for_s": 300},
        }))
        async with factory() as s, s.begin():
            await policy_service.revoke_publication(s, candidate["policy_id"], CHRIS)
        with pytest.raises(policy_service.PublishRejected) as e:
            async with factory() as s, s.begin():
                await policy_service.publish_version(s, candidate["version_id"], CHRIS)
        assert any(c["code"] == "E_CROSS_POLICY_LOOP" for c in e.value.conflicts)

    svc(go)


# ===== 模拟: CSV 的 zone_id 回填与高频警告 =====

def test_simulate_csv__zone_id_backfilled_so_open_incident_fires(svc):
    """第一段发现 events_from_csv 的事件没有 zone_id, 开事故类策略在真实数据上
    产出恒为 0 (SPEC-001 验收 8 的已知限制)。service 层规范化时从 sensors 表补上,
    补完之后 CSV 对全部动作类型可用 —— 这条测试证明补上了。"""
    async def go(factory):
        # 对照组: 不经 service、不回填, 同一策略同一数据产出恒为 0
        from scenario import load_source

        from policy_engine import LoadedPolicy, Policy, replay
        src = load_source("apps/device-sim/seed/waterlevel_readings.csv", None)
        bare = replay(
            [LoadedPolicy(policy_id=1, version=1, body=Policy.model_validate(GOOD_BODY))],
            src.events, source=src.name,
        )
        assert bare.by_action_type.get("open_incident", 0) == 0
        assert any(w.code == "W_NEVER_TRIGGERED" for w in bare.warnings)

        # service 路径: 回填后产出非零, 且 Effect 带上了 zone
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "csv-zone", GOOD_BODY, ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, created["version_id"])
        async with factory() as s, s.begin():
            report = await policy_service.simulate_version(
                s, created["version_id"], "apps/device-sim/seed/waterlevel_readings.csv"
            )
        assert report["by_action_type"]["open_incident"] > 0
        assert set(report["by_zone"]) <= {1, 2, 3} and report["by_zone"]
        assert not any(w["code"] == "W_NEVER_TRIGGERED" for w in report["warnings"])

    svc(go)


def test_simulate__high_trigger_rate_warns_but_does_not_block(svc):
    """触发过密给 W_HIGH_TRIGGER_RATE 但不阻断, 版本照常转 simulated (验收 4)。"""
    async def go(factory):
        body = {
            **GOOD_BODY,
            "scope": {"type": "sensor", "ids": [1, 2]},  # 每传感器独立冷却桶, 触发更密
        }
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "too-hot", body, ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, created["version_id"])
        async with factory() as s, s.begin():
            report = await policy_service.simulate_version(
                s, created["version_id"], "multi_sensor_escalation"
            )
        assert any(w["code"] == "W_HIGH_TRIGGER_RATE" for w in report["warnings"])
        assert await _version_status(factory, created["version_id"]) == "simulated"

    svc(go)


def test_simulate__rejects_source_outside_repo(svc):
    async def go(factory):
        async with factory() as s, s.begin():
            created = await policy_service.create_policy(s, "escape", GOOD_BODY, ALEX.id)
        async with factory() as s, s.begin():
            await policy_service.validate_version(s, created["version_id"])
        for source in ("../outside.yaml", "/etc/passwd", "no_such_scenario"):
            with pytest.raises(policy_service.SourceNotFound):
                async with factory() as s, s.begin():
                    await policy_service.simulate_version(s, created["version_id"], source)

    svc(go)


def test_create__schema_invalid_body_rejected(svc):
    """白名单以外的动作在 Schema 层直接失败, 连草稿都不建 (SPEC-001 不变量)。"""
    async def go(factory):
        bad = {**GOOD_BODY, "actions": [{"type": "delete_database"}]}
        with pytest.raises(policy_service.InvalidPolicyBody):
            async with factory() as s, s.begin():
                await policy_service.create_policy(s, "evil", bad, ALEX.id)
        async with factory() as s:
            count = (await s.execute(text("SELECT count(*) FROM policies"))).scalar_one()
        assert count == 0

    svc(go)
