"""花钱护栏的 HTTP 层 (SPEC-009 第二节验收): 三种 429 分得开、去重不重复扣、
并发由数据库串行化、回补在任务停止调用后到账。

打桩模型不走 cassette (与 test_agent_http 同一理由): 这里测的是预扣/配额/回补
与状态码, 与模型输出无关。

额度耗尽那条**把 limit 直接设成 0 来测**, 不等它自然花完 —— "从没超过"与
"检查失效"在外面看长得一模一样 (SPEC-009 第九节第 6 条; 本项目第八次记这件事)。
"""
# import 进来的 pytest 夹具 (llm / bo_headers) 再出现在用例形参里, ruff 会当成
# 重定义 —— 这是 pytest 的固定用法, 整文件豁免 F811
# ruff: noqa: F811
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_agent_helpers import clean_agent_tables, db  # noqa: F401
from test_agent_http import (  # noqa: F401  (llm/bo_headers 是夹具, import 即生效)
    ALEX,
    BO,
    bo_headers,
    happy_script,
    llm,
    say,
    wait_status,
)

from app.config import settings
from app.services import agent_runtime, budget_service


async def _counts(conn):
    return {
        "tasks": await conn.fetchval("SELECT count(*) FROM agent_tasks"),
        "spend_rows": await conn.fetchval("SELECT count(*) FROM llm_spend_daily"),
        "quota_rows": await conn.fetchval(
            "SELECT count(*) FROM user_task_quota_daily"
        ),
    }


def _spent() -> float | None:
    async def go(conn):
        return await conn.fetchval("SELECT spent_cny FROM llm_spend_daily")

    value = db(go)
    return None if value is None else float(value)


def _wait_spent(expected: float, timeout: float = 5.0) -> float:
    """回补挂在后台协程的 done_callback 上, 独立事务 —— 轮询等它到账。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = _spent()
        if got is not None and got == pytest.approx(expected):
            return got
        time.sleep(0.05)
    raise AssertionError(f"台账没等到 {expected}, 现在是 {_spent()}")


# ===== 三种 429: 码与文案都分得开 =====


def test_budget_zero__429_daily_budget_and_nothing_written(
    client, auth_headers, llm, monkeypatch
):
    """变异 1 (去掉 CHECK) 与变异 6 (判据恒真) 的正主: limit=0, 第一条任务就 429。"""
    monkeypatch.setattr(settings(), "llm_daily_budget_cny", 0.0)
    llm(happy_script())
    r = client.post("/agent-tasks", json={"text": "预算为零时的任务"},
                    headers=auth_headers(ALEX))
    assert r.status_code == 429, r.text
    assert r.headers["x-error-code"] == "daily_budget_exhausted"
    assert "额度已用完" in r.json()["detail"]
    # 任务行、预扣、配额全部随事务回滚 —— 429 不留任何痕迹
    assert db(_counts) == {"tasks": 0, "spend_rows": 0, "quota_rows": 0}
    # 槽位也退回了, 不然几次 429 之后系统就"忙"死了
    assert agent_runtime.running_task_count() == 0


def test_user_quota__next_task_rejected_but_other_user_ok(
    client, auth_headers, bo_headers, llm, monkeypatch
):
    monkeypatch.setattr(settings(), "agent_user_daily_tasks", 2)
    headers = auth_headers(ALEX)
    for i in range(2):
        llm(happy_script())
        r = client.post("/agent-tasks", json={"text": f"配额用例第 {i} 条"},
                        headers=headers)
        assert r.status_code == 201, r.text
        wait_status(client, headers, r.json()["task_id"], {"awaiting_approval"})

    denied = client.post("/agent-tasks", json={"text": "配额用例第 3 条"},
                         headers=headers)
    assert denied.status_code == 429, denied.text
    assert denied.headers["x-error-code"] == "user_quota_exhausted"
    assert "任务数已用完" in denied.json()["detail"]

    # 换一个账号仍可用 (验收第七条): 配额按人, 不是全站
    llm(happy_script())
    ok = client.post("/agent-tasks", json={"text": "另一个账号的任务"},
                     headers=bo_headers)
    assert ok.status_code == 201, ok.text
    wait_status(client, bo_headers, ok.json()["task_id"], {"awaiting_approval"})


def test_capacity__429_carries_its_own_code(client, auth_headers, monkeypatch):
    """槽位满与额度用完是两句话: 一个"等会儿再来", 一个"明天再来"。"""
    monkeypatch.setattr(settings(), "agent_max_concurrent_tasks", 0)
    r = client.post("/agent-tasks", json={"text": "槽位为零时的任务"},
                    headers=auth_headers(ALEX))
    assert r.status_code == 429
    assert r.headers["x-error-code"] == "capacity_exceeded"
    assert "上界" in r.json()["detail"]


# ===== 去重命中不扣钱 =====


def test_dedup_hit__charges_exactly_once(client, auth_headers, llm):
    llm(happy_script(), delay_s=0.5)
    headers = auth_headers(ALEX)
    first = client.post("/agent-tasks", json={"text": "去重不重复扣费"},
                        headers=headers)
    assert first.status_code == 201
    dup = client.post("/agent-tasks", json={"text": "去重不重复扣费"},
                      headers=headers)
    assert dup.status_code == 200 and dup.json()["created"] is False

    # 命中去重的那次什么都没多得到, 台账上只有第一次的预扣
    assert _spent() == pytest.approx(settings().agent_task_hold_cny)

    async def quota(conn):
        return await conn.fetchval(
            "SELECT used FROM user_task_quota_daily WHERE user_id = 3"
        )

    assert db(quota) == 1
    wait_status(client, headers, first.json()["task_id"], {"awaiting_approval"})


# ===== 并发: 预扣在事务里, 由数据库串行化 (变异 2 的正主) =====


def test_concurrent_creates__only_holds_within_budget_succeed(
    client, auth_headers, llm, monkeypatch
):
    """十条并发只有额度内的三条能建起来 (验收第六条)。

    把预扣改成"先查余额, 够就扣"的话, 十个请求同时读到"还有余额",
    创建数会超过 3 —— 这条测试就红。
    """
    cfg = settings()
    monkeypatch.setattr(cfg, "llm_daily_budget_cny", 1.8)  # 正好 3 笔预扣
    monkeypatch.setattr(cfg, "agent_task_hold_cny", 0.6)
    monkeypatch.setattr(cfg, "agent_user_daily_tasks", 1000)
    monkeypatch.setattr(cfg, "agent_max_concurrent_tasks", 50)
    # 延迟 1 秒: 保证没有任务在创建风暴结束前跑完并回补 (回补会释放额度,
    # 让第 4 条挤进来 —— 那测的就不是并发预扣了)
    llm([say("并发用例")] * 40, delay_s=1.0)
    headers = auth_headers(ALEX)  # 线程外先登录, 线程里只发创建请求

    def create(i: int):
        return client.post("/agent-tasks", json={"text": f"并发建任务 {i}"},
                           headers=headers)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(create, range(10)))

    ok = [r for r in results if r.status_code == 201]
    denied = [r for r in results if r.status_code == 429]
    assert len(ok) == 3, [r.status_code for r in results]
    assert len(denied) == 7
    assert all(
        r.headers["x-error-code"] == "daily_budget_exhausted" for r in denied
    )

    async def count(conn):
        return await conn.fetchval("SELECT count(*) FROM agent_tasks")

    assert db(count) == 3

    # 收尾: 等三条后台轮走完 (脚本只有文本, compiling 处协议错落 failed),
    # 不让后台协程外溢到下一个用例
    for r in ok:
        wait_status(client, headers, r.json()["task_id"],
                    {"failed", "awaiting_approval", "dead_letter"}, timeout=20)


# ===== 回补: 任务不再调用后, 差额回到台账 =====


def test_refund__round_end_callback_then_sweep_settle_decreases_once(
    client, auth_headers, llm, svc
):
    """轮次收尾 (done_callback) 与清扫对同一条任务各结算一次, 台账只减一次。

    台账预置另一笔 0.6 (等价于还有一条任务在飞): 正确实现停在 0.6;
    "每次都回补"的实现会把别人的预扣也退掉, 减到 0.0。清扫侧用的就是
    budget_service.refund_task_hold (agent_runtime.sweep_once 逐条调它)。
    """
    async def seed(conn):
        await conn.execute(
            "INSERT INTO llm_spend_daily (day, spent_cny, limit_cny) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, 0.6, 10.0)"
        )

    db(seed)
    llm(happy_script())
    headers = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "先收尾后清扫"}, headers=headers)
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    wait_status(client, headers, task_id, {"awaiting_approval"})
    _wait_spent(0.6)  # 本任务的 0.6 已由收尾回调结算, 预置的那笔还在

    async def sweep_settle(factory):
        async with factory() as session, session.begin():
            await budget_service.refund_task_hold(session, task_id)

    svc(sweep_settle)
    assert _spent() == pytest.approx(0.6)  # 钥匙已被收尾回调抢走, 清扫侧 0 行更新


def test_refund__hold_returns_after_task_stops_calling(client, auth_headers, llm):
    llm(happy_script())
    headers = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "回补用例"}, headers=headers)
    assert r.status_code == 201
    wait_status(client, headers, r.json()["task_id"], {"awaiting_approval"})

    # 打桩调用成本 0 -> 全额回补; awaiting_approval 之后审批批不批都不再调模型
    _wait_spent(0.0)

    async def quota(conn):
        return await conn.fetchval(
            "SELECT used FROM user_task_quota_daily WHERE user_id = 3"
        )

    assert db(quota) == 1  # 配额不回补: 它数的是"建过几条任务", 不是钱
