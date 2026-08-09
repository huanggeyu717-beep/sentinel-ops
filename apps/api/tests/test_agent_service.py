"""agent_service: 去重、租约与栅栏、时间线编号、多轮澄清 (SPEC-002 验收
9/10/12/13/14/15 的 service 层半边; 直接写库的另一半在 test_agent_constraints)。
"""
from __future__ import annotations

import asyncio

import pytest
from test_agent_helpers import clean_agent_tables, db  # noqa: F401

from app.services import agent_service, policy_service

OWNER = 3      # alex (operator), 种子账号
OTHER_OP = 2   # chris —— 同样有 operator 权限, 但不是发起人


async def _open_task(factory, *, text="生鲜区两个探头都湿了就通知主管", user_id=OWNER):
    async with factory() as session, session.begin():
        created = await agent_service.create_task(
            session, user_id=user_id, input_text=text
        )
    return created


async def _claim(factory, task_id, runner="runner-A"):
    async with factory() as session, session.begin():
        assert await agent_service.claim_task(session, task_id, runner)
    return runner


# ===== 归一化 =====


def test_normalize_input__collapses_whitespace_and_binds_policy():
    n1, h1 = agent_service.normalize_input("  把冷却\n改成   600 秒 ", None)
    n2, h2 = agent_service.normalize_input("把冷却 改成 600 秒", None)
    assert n1 == n2 and h1 == h2
    # 同一句话用在两条不同策略上是两件事, 只哈希文本会误挡 (SPEC-002 第七节)
    _, h_policy_7 = agent_service.normalize_input("把冷却 改成 600 秒", 7)
    assert h_policy_7 != h1


# ===== 验收 12: 并发提交只开出一个任务 =====


def test_create_task__concurrent_duplicate_submissions_open_one_task(svc):
    async def go(factory):
        results = await asyncio.gather(
            _open_task(factory), _open_task(factory)
        )
        return results

    r1, r2 = svc(go)
    assert {r1["created"], r2["created"]} == {True, False}
    assert r1["task_id"] == r2["task_id"]


# ===== 验收 13: 上一条停在 clarifying 时重说 -> 拿回那一条, 引导去回答 =====


def test_create_task__while_previous_clarifying_returns_it(svc):
    async def go(factory):
        first = await _open_task(factory)
        runner = await _claim(factory, first["task_id"])
        async with factory() as session, session.begin():
            await agent_service.ask_clarification(
                session, first["task_id"], runner, "通知哪个角色?"
            )
        again = await _open_task(factory)
        return first, again

    first, again = svc(go)
    assert again["created"] is False
    assert again["task_id"] == first["task_id"]
    assert again["status"] == "clarifying"


# ===== 验收 14 + 崩溃窗口: 终态可重开; 打卡停了的 running 标"疑似中断" =====


def test_create_task__reopens_after_terminal_and_flags_interrupted(svc):
    async def go(factory):
        first = await _open_task(factory)
        # 疑似中断: running 且打卡已停超过判死线, 但清扫还没到 —— 不报"重复提交"
        async with factory() as session, session.begin():
            await agent_service.claim_task(session, first["task_id"], "runner-A")
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_tasks SET heartbeat_at = now() - interval '90 seconds' "
                "WHERE id = :id"), {"id": first["task_id"]},
            )
        stuck = await _open_task(factory)
        # 任务走到终态之后, 同样输入能开出新任务
        async with factory() as session, session.begin():
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_tasks SET status = 'completed' WHERE id = :id"),
                {"id": first["task_id"]},
            )
        reopened = await _open_task(factory)
        return first, stuck, reopened

    first, stuck, reopened = svc(go)
    assert stuck["created"] is False and stuck["suspected_interrupted"] is True
    assert reopened["created"] is True
    assert reopened["task_id"] != first["task_id"]


def test_create_task__different_inputs_not_deduped(svc):
    async def go(factory):
        a = await _open_task(factory, text="生鲜区漏水开单")
        b = await _open_task(factory, text="后场漏水点灯")
        return a, b

    a, b = svc(go)
    assert a["created"] and b["created"] and a["task_id"] != b["task_id"]


# ===== 两张表一条编号: 时间线 seq 唯一且连续 =====


def test_timeline__steps_and_clarifications_share_one_ordered_seq(svc):
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        runner = await _claim(factory, task_id)
        async with factory() as session, session.begin():
            await agent_service.record_step(
                session, task_id, runner, tool_name="list_zones",
                result_summary={"count": 3},
            )
            await agent_service.record_step(
                session, task_id, runner, tool_name="list_sensors",
                result_summary={"count": 5},
            )
            await agent_service.ask_clarification(session, task_id, runner, "哪个区?")
        async with factory() as session, session.begin():
            await agent_service.answer_clarification(session, task_id, OWNER, "生鲜区")
        # 恢复后继续长
        await _claim(factory, task_id, "runner-B")
        async with factory() as session, session.begin():
            await agent_service.record_step(
                session, task_id, "runner-B", tool_name="list_zones",
                result_summary={"count": 3},
            )
            await agent_service.advance_stage(session, task_id, "runner-B", "compiling")
            timeline = await agent_service.get_timeline(session, task_id)
        return timeline

    timeline = svc(go)
    seqs = [t["seq"] for t in timeline]
    assert seqs == list(range(1, len(seqs) + 1)), seqs  # 连续、唯一、可排序
    kinds = [t["kind"] for t in timeline]
    assert "clarification_question" in kinds and "clarification_answer" in kinds
    # 状态迁移与工具调用在时间线上分得开, Trace UI 不会把"进入 compiling"画成工具
    assert "transition" in kinds and "step" in kinds
    for t in timeline:
        assert (t["kind"] == "transition") == (t["label"] == "stage_transition")


# ===== 验收 9: 只有发起人本人能回答 =====


def test_answer_clarification__non_initiator_rejected(svc):
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        runner = await _claim(factory, task_id)
        async with factory() as session, session.begin():
            await agent_service.ask_clarification(session, task_id, runner, "哪个区?")
        with pytest.raises(agent_service.NotTaskOwner):
            async with factory() as session, session.begin():
                await agent_service.answer_clarification(
                    session, task_id, OTHER_OP, "我替他答"
                )
        # 发起人本人答得进去, 任务回 running、stage 回 discovering
        async with factory() as session, session.begin():
            await agent_service.answer_clarification(session, task_id, OWNER, "生鲜区")
            task = await agent_service.get_task(session, task_id)
        return task

    task = svc(go)
    assert task["status"] == "running" and task["stage"] == "discovering"
    assert task["runner_id"] is None  # 租约交还, 恢复执行的进程重新认领


# ===== 验收 10 (应用层半边): 并发回答两次只有一个成功 =====


def test_answer_clarification__concurrent_answers_one_wins(svc):
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        runner = await _claim(factory, task_id)
        async with factory() as session, session.begin():
            await agent_service.ask_clarification(session, task_id, runner, "哪个区?")

        async def answer(text):
            try:
                async with factory() as session, session.begin():
                    await agent_service.answer_clarification(
                        session, task_id, OWNER, text
                    )
                return "ok"
            except agent_service.TransitionConflict:
                return "conflict"

        return await asyncio.gather(answer("生鲜区"), answer("后场"))

    outcomes = svc(go)
    assert sorted(outcomes) == ["conflict", "ok"]


# ===== 验收 15 前半: 失联判死; 后半: 那道闸让复活的写零行、零草稿 =====


def test_reap__stale_heartbeat_becomes_dead_letter(svc):
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        await _claim(factory, task_id, "runner-A")
        async with factory() as session, session.begin():
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_tasks SET heartbeat_at = now() - interval '120 seconds' "
                "WHERE id = :id"), {"id": task_id},
            )
            reaped = await agent_service.reap(
                session, lease_timeout_seconds=60, task_ttl_hours=24
            )
        async with factory() as session, session.begin():
            task = await agent_service.get_task(session, task_id)
        return task, reaped

    task, reaped = svc(go)
    assert task["status"] == "dead_letter"
    assert task["error_code"] == "lease_timeout"
    assert task["error_detail"]  # 人话解释, 不是空的
    # reap() 必须把这个 id 报出来 —— 调用方靠这个返回值去把草稿标 discarded
    assert any(r["task_id"] == task["id"] for r in reaped)


# ===== 验收 15c: 清扫的两个分支各一条 (第一段只测了打卡过期那一半) =====


def test_reap__never_claimed_task_over_threshold_becomes_dead_letter(svc):
    """兜底半边: 进程在建任务与认领之间死掉, 任务从没被打过卡。
    新建即盖第一次心跳戳, 过阈值没人认领照样判死; running 却没有心跳戳
    (不变量已破) 视为立即失联。"""
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        # 从没认领过 (runner_id 为 NULL), 模拟时间流逝 120 秒 > 阈值 60 秒
        async with factory() as session, session.begin():
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_tasks SET heartbeat_at = heartbeat_at - interval "
                "'120 seconds' WHERE id = :id"), {"id": task_id},
            )
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)
        aged = await _task(factory, task_id)

        # 不变量兜底: running 却心跳戳为 NULL 的行 (只能来自代码回归或手改库) 即失联
        async with factory() as session, session.begin():
            from sqlalchemy import text
            broken = (await session.execute(text(
                "INSERT INTO agent_tasks (user_id, task_type, input, input_hash) "
                "VALUES (3, 'policy_compile', '{}'::jsonb, 'null-heartbeat') "
                "RETURNING id"
            ))).scalar_one()
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)
        return aged, await _task(factory, broken)

    aged, broken = svc(go)
    assert aged["status"] == "dead_letter" and aged["error_code"] == "lease_timeout"
    assert broken["status"] == "dead_letter"
    # 两格的 error_detail 必须分开 (SPEC-002 第二节): NULL 戳的含义是"有人让任务
    # 处于 running 却忘了盖戳", 与重启毫无关系 —— 沿用"可能是服务重启"那句,
    # 排查的人会去翻根本没发生过的重启记录
    assert "失联" in aged["error_detail"] and "心跳戳缺失" not in aged["error_detail"]
    assert "心跳戳缺失" in broken["error_detail"]
    assert "重启" not in broken["error_detail"]


def test_reap__freshly_created_unclaimed_task_survives_sweep(svc):
    """验收 15c 第三条 / 变异 22c 的靶: 刚新建、还没被认领的任务立刻跑一次清扫
    -> 必须还是 running。守的是**新建那一处盖戳**。

    不能靠"建完老化再清扫"那条代劳: 新建若不盖戳, heartbeat_at 是 NULL,
    `NULL - interval` 仍是 NULL, 而 NULL 本来就立即判死 —— 老化那条测试在
    "新建不盖戳"这个变异下照样绿 (第一段修补后复核实测, SPEC-002 验收 15c)。"""
    async def go(factory):
        created = await _open_task(factory)
        async with factory() as session, session.begin():
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)
        return await _task(factory, created["task_id"])

    task = svc(go)
    assert task["status"] == "running", (
        f"刚新建的任务被清扫误杀了: {task['status']} / {task['error_detail']}"
    )
    assert task["heartbeat_at"] is not None  # 新建即盖第一次戳


async def _task(factory, task_id):
    async with factory() as session, session.begin():
        return await agent_service.get_task(session, task_id)


def test_reap__answered_task_has_fresh_grace_period(svc):
    """出过事的那条 (SPEC-002 第二节实测复现): created_at 十分钟前的老任务,
    被人回答后刚变回 running, 跑一次清扫必须**还是 running** —— 变回 running 的
    写操作同时打了卡, 宽限期从回答那一刻重新起算, 不从 created_at 起算。
    这条也是变异 22b 的靶: 把"变回 running 时打一次卡"去掉, 它必须红。"""
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        runner = await _claim(factory, task_id)
        async with factory() as session, session.begin():
            from sqlalchemy import text
            # 老任务: 建于十分钟前
            await session.execute(text(
                "UPDATE agent_tasks SET created_at = now() - interval '10 minutes' "
                "WHERE id = :id"), {"id": task_id},
            )
            await agent_service.ask_clarification(session, task_id, runner, "哪个区?")
        # 人现在才回答, 任务刚变回 running
        async with factory() as session, session.begin():
            await agent_service.answer_clarification(session, task_id, OWNER, "生鲜区")
        async with factory() as session, session.begin():
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)
        fresh = await _task(factory, task_id)

        # 宽限期是真的但有界: 回答后 120 秒仍没进程认领续跑 -> 照样判死
        async with factory() as session, session.begin():
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_tasks SET heartbeat_at = heartbeat_at - interval "
                "'120 seconds' WHERE id = :id"), {"id": task_id},
            )
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)
        return fresh, await _task(factory, task_id)

    fresh, later = svc(go)
    assert fresh["status"] == "running", (
        f"刚被回答的任务被清扫误杀了: {fresh['status']} / {fresh['error_code']}"
    )
    assert later["status"] == "dead_letter" and later["error_code"] == "lease_timeout"


def test_lease_gate__zombie_write_produces_no_rows_and_no_draft(svc):
    """假死复活: 判死之后, 老 runner 的整笔写入 (草稿 + 步骤) 因闸受影响 0 行
    而整体回滚 —— 直接断言"没有第二份草稿", 不只断言状态 (验收 15)。"""
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        await _claim(factory, task_id, "runner-A")
        async with factory() as session, session.begin():
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_tasks SET heartbeat_at = now() - interval '120 seconds' "
                "WHERE id = :id"), {"id": task_id},
            )
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)

        # 僵住的进程醒了, 以为自己还在第 5 步: 同一事务里建草稿 + 落步骤
        zombie_wrote = None
        with pytest.raises(agent_service.LeaseLost):
            async with factory() as session, session.begin():
                zombie_wrote = await policy_service.create_policy(
                    session, name="zombie-draft",
                    body={"scope": {"type": "zone", "ids": [1]},
                          "trigger": {"type": "sensor_state_changed", "to": "WET"},
                          "conditions": [],
                          "actions": [{"type": "open_incident", "severity": "normal"}],
                          "cooldown_s": 60},
                    created_by=OWNER, source="agent",
                )
                await agent_service.record_step(
                    session, task_id, "runner-A", tool_name="create_policy",
                    result_summary=zombie_wrote,
                )
        assert zombie_wrote is not None  # 草稿语句执行过, 但必须随事务一起消失

        async with factory() as session, session.begin():
            from sqlalchemy import text
            drafts = (await session.execute(text(
                "SELECT count(*) FROM policies WHERE name = 'zombie-draft'"
            ))).scalar_one()
            steps = (await session.execute(text(
                "SELECT count(*) FROM agent_steps WHERE task_id = :id"),
                {"id": task_id},
            )).scalar_one()
        return drafts, steps

    drafts, steps = svc(go)
    assert drafts == 0  # 一个字都没写进去
    assert steps == 0


def test_lease_gate__stale_runner_rejected_even_while_task_running(svc):
    """判别性测试 —— 只有 runner_id 条件能拦的场景 (验收 15 那条拦不住这一种)。

    验收 15 的场景里任务已被判成 dead_letter, status 条件与 runner_id 条件重叠,
    单独拆掉 runner_id 那半边闸, 那条测试照样绿 (与 SPEC-001 验收 6b 同一课)。
    这里构造 status 仍是 running、但主人已换的情况: A 提问后僵住, 人回答,
    新进程 B 认领续跑 —— A 醒来写库, 唯一拦得住它的就是 runner_id 对不上。
    """
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        await _claim(factory, task_id, "runner-A")
        async with factory() as session, session.begin():
            await agent_service.ask_clarification(session, task_id, "runner-A", "哪个区?")
        # A 在这里僵住; 人回答, 任务回 running, 新进程 B 认领接着跑
        async with factory() as session, session.begin():
            await agent_service.answer_clarification(session, task_id, OWNER, "生鲜区")
        await _claim(factory, task_id, "runner-B")

        # A 醒了, 以为任务还是自己的: status='running' 是真的, 主人不是它了
        with pytest.raises(agent_service.LeaseLost):
            async with factory() as session, session.begin():
                await agent_service.record_step(
                    session, task_id, "runner-A", tool_name="list_zones",
                    result_summary={"count": 3},
                )
        async with factory() as session, session.begin():
            from sqlalchemy import text
            steps = (await session.execute(text(
                "SELECT count(*) FROM agent_steps WHERE task_id = :id"),
                {"id": task_id},
            )).scalar_one()
        return steps

    assert svc(go) == 0  # A 的写一行都没落下


def test_reap__clarifying_over_ttl_becomes_dead_letter(svc):
    """clarifying 不打卡, 由生存期上限收尾 (验收 11 后半)。"""
    async def go(factory):
        created = await _open_task(factory)
        task_id = created["task_id"]
        runner = await _claim(factory, task_id)
        async with factory() as session, session.begin():
            await agent_service.ask_clarification(session, task_id, runner, "哪个区?")
        async with factory() as session, session.begin():
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE agent_clarifications SET asked_at = now() - interval '25 hours' "
                "WHERE task_id = :id"), {"id": task_id},
            )
            await agent_service.reap(session, lease_timeout_seconds=60, task_ttl_hours=24)
        async with factory() as session, session.begin():
            return await agent_service.get_task(session, task_id)

    task = svc(go)
    assert task["status"] == "dead_letter"
    assert task["error_code"] == "clarify_timeout"
    assert "澄清" in task["error_detail"]
