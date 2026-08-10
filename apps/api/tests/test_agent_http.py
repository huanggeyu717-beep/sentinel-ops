"""HTTP 层: 路由 + SSE (SPEC-002 验收 2、16、18 与变异 21 的钉子在本文件结清)。

**打桩模型, 不走 cassette** (本段易错点七): 路由层要验的是"立刻返回 / 权限 /
状态码 / SSE 续传", 与模型输出无关; 打桩还能精确控制耗时 —— 验收 2 靠打桩
故意慢一拍, 断言 POST 的响应时间远小于任务完成时间, 用真模型测这个只会飘。

SSE 测试直接读 TestClient 的流式响应逐行解析 —— 不 mock 服务端的生成器,
断线重连测的就是"真的从 Last-Event-ID 之后接着推"。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import pytest
from test_agent_helpers import clean_agent_tables, db, insert_task  # noqa: F401

from app.config import settings
from app.services import agent_runtime, agent_service
from app.services.llm_client import (
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    ScriptedLLMClient,
)

ALEX = "alex@example.com"    # operator, 种子 user 3, 任务发起人
BO = "bo@example.com"        # operator, 非发起人 (验收 9; 种子用户里没有第二个
#                              operator, 由下面的 bo_headers 夹具入库)
CHRIS = "chris@example.com"  # manager, 审批与发布

VALID_BODY = {
    "scope": {"type": "zone", "ids": [1]},
    "trigger": {"type": "sensor_state_changed", "to": "WET"},
    "conditions": [],
    "actions": [{"type": "open_incident", "severity": "normal"}],
    "cooldown_s": 60,
}


def tool(_tool_name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(tool_call=LLMToolCall(tool=_tool_name, arguments=arguments),
                       input_tokens=10, output_tokens=5)


def say(content: str) -> LLMResponse:
    return LLMResponse(text=content, input_tokens=10, output_tokens=5)


def happy_script() -> list[LLMResponse]:
    return [say("在 1 区变湿时开事故"),
            tool("create_policy", name="HTTP 测试策略", body=VALID_BODY)]


def clarify_script() -> list[LLMResponse]:
    return [say("要开单, 但没说通知谁"),
            tool("ask_clarification", question="触发后要通知哪个角色?",
                 missing_slots=["role"])]


class SlowLLM:
    """打桩 + 可控延迟: 验收 2 的"任务确实在后台慢慢跑"就靠 delay_s。"""

    def __init__(self, script: list[LLMResponse], delay_s: float = 0.0) -> None:
        self.inner = ScriptedLLMClient(script=list(script))
        self.delay_s = delay_s

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return await self.inner.complete(request)


@pytest.fixture
def llm(client):
    """把路由层的模型客户端换成打桩 (FastAPI dependency_overrides)。

    返回 use(script, delay_s): 每次调用替换"下一次 spawn 会拿到的客户端" ——
    澄清回来那一轮经 reply 重新走 get_llm_client, 测试给它换第二段脚本。
    """
    from app.main import app
    from app.routers import agent_tasks as agent_tasks_router

    current: dict[str, Any] = {}

    def use(script: list[LLMResponse], delay_s: float = 0.0) -> SlowLLM:
        current["llm"] = SlowLLM(script, delay_s)
        return current["llm"]

    app.dependency_overrides[agent_tasks_router.get_llm_client] = (
        lambda: current["llm"]
    )
    yield use
    app.dependency_overrides.pop(agent_tasks_router.get_llm_client, None)


@pytest.fixture
def bo_headers(client, auth_headers):
    """入库第二个 operator 账号 (验收 9 要"同为 operator 的别人"), 手法照
    conftest.viewer_headers: 幂等插入, 不挂员工。"""
    from conftest import SEED_PASSWORD

    from app.services import auth_service

    password_hash = auth_service.hash_password(SEED_PASSWORD)

    async def go(conn):
        await conn.execute(
            "INSERT INTO users (email, password_hash, display_name) "
            "VALUES ($1, $2, 'Bo Wang') ON CONFLICT (email) DO NOTHING",
            BO, password_hash,
        )
        await conn.execute(
            "INSERT INTO user_roles (user_id, role_id) "
            "SELECT u.id, r.id FROM users u, roles r "
            "WHERE u.email = $1 AND r.name = 'operator' ON CONFLICT DO NOTHING",
            BO,
        )

    db(go)
    return auth_headers(BO)


@pytest.fixture(autouse=True)
def no_leftover_background(client):
    """用例结束后等后台协程清零: 残留协程会与下一个用例的 TRUNCATE 竞态。"""
    yield
    deadline = time.monotonic() + 10
    while agent_runtime.running_task_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert agent_runtime.running_task_count() == 0, "后台任务没清干净"


def wait_status(client, headers, task_id: int, statuses: set[str],
                timeout: float = 10.0) -> dict[str, Any]:
    """轮询快照直到任务进入指定状态之一, 返回整个快照。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/agent-tasks/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        snap = r.json()
        if snap["task"]["status"] in statuses:
            return snap
        time.sleep(0.03)
    raise AssertionError(f"任务 {task_id} 超时未进入 {statuses}")


# ===== 验收 2: POST 立刻返回, 任务在后台跑 =====


def test_post_returns_immediately__stub_deliberately_slow(client, auth_headers, llm):
    """打桩每次调用故意睡 0.6 秒 (两次调用 -> 任务至少 1.2 秒), POST 必须远快于它。"""
    llm(happy_script(), delay_s=0.6)
    headers = auth_headers(ALEX)

    t0 = time.perf_counter()
    r = client.post("/agent-tasks", json={"text": "验收2 慢打桩输入"}, headers=headers)
    post_elapsed = time.perf_counter() - t0
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and body["status"] == "running"

    # 拿到响应的这一刻任务还没跑完 —— "立刻返回"不是"跑完才返回"
    snap = client.get(f"/agent-tasks/{body['task_id']}", headers=headers).json()
    assert snap["task"]["status"] == "running"

    wait_status(client, headers, body["task_id"], {"awaiting_approval"})
    task_elapsed = time.perf_counter() - t0
    assert post_elapsed < 0.5, f"POST 花了 {post_elapsed:.3f}s, 不算立刻返回"
    assert task_elapsed >= 1.2, f"任务 {task_elapsed:.3f}s 就完了, 打桩延迟没生效"
    # 实测数字进完成报告 (验收 2 要报 POST 耗时 vs 任务完成耗时)
    print(f"\n[验收2] post={post_elapsed * 1000:.1f}ms "
          f"task_total={task_elapsed:.2f}s (delay_s=0.6 x 2 calls)")


# ===== 快照与 Trace 字段 =====


def test_snapshot__timeline_carries_tool_latency_tokens_and_stage(
    client, auth_headers, llm
):
    llm(happy_script())
    headers = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "快照字段输入"}, headers=headers)
    snap = wait_status(client, headers, r.json()["task_id"], {"awaiting_approval"})

    items = snap["timeline"]
    kinds = {i["kind"] for i in items}
    assert {"transition", "step"} <= kinds
    # transition 要能显示去哪 (Trace UI 的招牌): arguments.to
    transitions = [i for i in items if i["kind"] == "transition"]
    assert all(i["arguments"] and "to" in i["arguments"] for i in transitions)
    # 工具步骤带耗时; 由模型驱动的那步带 token 数
    create = next(i for i in items if i["label"] == "create_policy")
    assert create["latency_ms"] is not None
    assert create["input_tokens"] == 10 and create["output_tokens"] == 5
    # seq 是一条不重不漏的时间线
    seqs = [i["seq"] for i in items]
    assert seqs == list(range(1, len(seqs) + 1))


def test_permissions__viewer_reads_but_cannot_submit(
    client, auth_headers, viewer_headers, llm
):
    llm(happy_script())
    headers = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "viewer 权限输入"}, headers=headers)
    task_id = r.json()["task_id"]
    wait_status(client, headers, task_id, {"awaiting_approval"})

    assert client.get(f"/agent-tasks/{task_id}", headers=viewer_headers).status_code == 200
    assert client.post("/agent-tasks", json={"text": "x"},
                       headers=viewer_headers).status_code == 403
    assert client.post("/agent-tasks", json={"text": "x"}).status_code == 401
    assert client.get("/agent-tasks/999999", headers=headers).status_code == 404


# ===== 去重: 撞索引不是错误 (SPEC-002 第二节) =====


def test_dedupe__second_post_gets_same_task_200_not_4xx(client, auth_headers, llm):
    llm(happy_script(), delay_s=0.5)
    headers = auth_headers(ALEX)
    first = client.post("/agent-tasks", json={"text": "去重输入"}, headers=headers)
    assert first.status_code == 201
    second = client.post("/agent-tasks", json={"text": "去重输入"}, headers=headers)
    # 手抖点两下: 第二次拿回正在跑的那一个, 200 而不是 4xx
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["created"] is False
    assert body["task_id"] == first.json()["task_id"]
    assert body["suspected_interrupted"] is False
    wait_status(client, headers, body["task_id"], {"awaiting_approval"})


def test_dedupe__stale_heartbeat_flagged_suspected_interrupted(client, auth_headers):
    """服务刚崩溃的 60 秒窗口: 返回卡住的任务并标"疑似中断", 不报"重复提交"。"""
    text_input = "疑似中断输入"
    _, input_hash = agent_service.normalize_input(text_input, None)

    async def seed(conn):
        return await insert_task(
            conn, user_id=3, input_hash=input_hash, status="running",
            runner_id="dead-runner",
        )

    stuck_id = db(seed)

    async def age(conn):
        await conn.execute(
            "UPDATE agent_tasks SET heartbeat_at = now() - interval '120 seconds' "
            "WHERE id = $1", stuck_id)

    db(age)
    r = client.post("/agent-tasks", json={"text": text_input},
                    headers=auth_headers(ALEX))
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == stuck_id and body["created"] is False
    assert body["suspected_interrupted"] is True


# ===== 并发上界: 超出 429, 不建行不开协程 =====


def test_capacity__429_when_at_bound_and_no_row_created(
    client, auth_headers, llm, monkeypatch
):
    monkeypatch.setattr(settings(), "agent_max_concurrent_tasks", 1)
    llm(happy_script(), delay_s=0.5)
    headers = auth_headers(ALEX)
    first = client.post("/agent-tasks", json={"text": "占满槽位的输入"}, headers=headers)
    assert first.status_code == 201

    denied = client.post("/agent-tasks", json={"text": "第二条不同的输入"},
                         headers=headers)
    assert denied.status_code == 429, denied.text
    assert "上界" in denied.json()["detail"]

    async def count(conn):
        return await conn.fetchval("SELECT count(*) FROM agent_tasks")

    assert db(count) == 1  # 429 那次没留下任何行

    wait_status(client, headers, first.json()["task_id"], {"awaiting_approval"})
    llm(happy_script(), delay_s=0.0)
    retry = client.post("/agent-tasks", json={"text": "第二条不同的输入"},
                        headers=headers)
    assert retry.status_code == 201  # 槽位随任务结束释放
    wait_status(client, headers, retry.json()["task_id"], {"awaiting_approval"})


# ===== 澄清: 回答 / 越权 / 撞车 =====


def test_reply__owner_resumes_same_task_and_trace_grows(client, auth_headers, llm):
    """验收 8 的 HTTP 半边: 回答后同一条任务继续, task_id 不变, Trace 接着长。"""
    llm(clarify_script())
    headers = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "含糊输入 HTTP 版"}, headers=headers)
    task_id = r.json()["task_id"]
    snap = wait_status(client, headers, task_id, {"clarifying"})
    asked_seqs = [i["seq"] for i in snap["timeline"]
                  if i["kind"] == "clarification_question"]
    assert len(asked_seqs) == 1

    # 恢复的一轮从 discovering 重走, 第一次模型调用是 compiling -> 换第二段脚本
    llm([tool("create_policy", name="澄清后编译", body=VALID_BODY)])
    reply = client.post(f"/agent-tasks/{task_id}/reply",
                        json={"answer": "通知 manager"}, headers=headers)
    assert reply.status_code == 200, reply.text
    assert reply.json()["task_id"] == task_id

    snap = wait_status(client, headers, task_id, {"awaiting_approval"})
    kinds = [i["kind"] for i in snap["timeline"]]
    assert "clarification_question" in kinds and "clarification_answer" in kinds
    answer = next(i for i in snap["timeline"] if i["kind"] == "clarification_answer")
    assert answer["seq"] > asked_seqs[0]  # 同一条编号, 答在问后


def test_reply__non_owner_403_and_task_untouched(client, auth_headers, bo_headers, llm):
    """验收 9 的 HTTP 半边: 只有发起人能回答, 同为 operator 的别人 403。"""
    llm(clarify_script())
    alex = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "别人不能替答的输入"}, headers=alex)
    task_id = r.json()["task_id"]
    wait_status(client, alex, task_id, {"clarifying"})

    denied = client.post(f"/agent-tasks/{task_id}/reply",
                         json={"answer": "我替他答"}, headers=bo_headers)
    assert denied.status_code == 403, denied.text
    snap = client.get(f"/agent-tasks/{task_id}", headers=alex).json()
    assert snap["task"]["status"] == "clarifying"  # 403 的回答一个字都没写进去
    assert agent_runtime.running_task_count() == 0  # 槽位也退回去了


def test_reply__answer_after_resume_conflicts_409(client, auth_headers, llm):
    llm(clarify_script())
    headers = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "重复回答的输入"}, headers=headers)
    task_id = r.json()["task_id"]
    wait_status(client, headers, task_id, {"clarifying"})

    llm([tool("create_policy", name="第一答编译", body=VALID_BODY)])
    assert client.post(f"/agent-tasks/{task_id}/reply", json={"answer": "答一"},
                       headers=headers).status_code == 200
    second = client.post(f"/agent-tasks/{task_id}/reply", json={"answer": "答二"},
                         headers=headers)
    assert second.status_code == 409, second.text
    wait_status(client, headers, task_id, {"awaiting_approval"})


# ===== SSE (验收 18: 按 seq 排唯一顺序, 断线重连不重复不遗漏) =====
#
# 断线重连测试跑在**真 uvicorn + 真 httpx 流**上, 不用 TestClient:
# starlette 的 TestClient 把整个响应缓冲完才交给调用方 (testclient.py 里
# response_complete.wait()), 一条还没结束的 SSE 流在它手里永远读不到第一个
# 字节 —— 拿它测"边跑边推"会直接挂住, 测终态任务的全量重放则没问题。
# 真服务器同时把"客户端断开 -> is_disconnected -> 生成器退出"这条路也测真了。


@pytest.fixture()
def sse_server(client):
    """起一个真 uvicorn (随机端口, 同一个 app 对象 -> dependency_overrides 与
    JWT 密钥都通用)。依赖 client 只为保证迁移与种子已跑完。"""
    import socket
    import threading

    import uvicorn
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as app_db
    from app.main import app

    # uvicorn 有自己的事件循环, 而 db 的默认引擎带连接池 —— asyncpg 连接绑定
    # 事件循环, TestClient 的循环养出的连接被 uvicorn 循环复用会炸。夹具期间
    # 换成 NullPool (用完即断, 不跨循环), 与 conftest.svc 同一道理; NullPool
    # 没有滞留连接, 结束后直接还原, 不需要 dispose。
    old_engine, old_factory = app_db._engine, app_db._session_factory
    null_engine = create_async_engine(
        os.environ["SENTINEL_DATABASE_URL"], poolclass=NullPool
    )
    app_db._engine = null_engine
    app_db._session_factory = async_sessionmaker(
        null_engine, expire_on_commit=False, class_=AsyncSession
    )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn 没起来"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app_db._engine, app_db._session_factory = old_engine, old_factory
        assert not thread.is_alive(), "uvicorn 没关干净"


def read_sse(resp, *, stop, deadline_s: float = 10.0) -> list[dict[str, Any]]:
    """逐行解析 SSE 流, 每个事件收成 {id, event, data}; stop(events) 为真即返回。"""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    deadline = time.monotonic() + deadline_s
    for line in resp.iter_lines():
        assert time.monotonic() < deadline, f"SSE 读超时, 已收 {len(events)} 个事件"
        if line.startswith("id:"):
            current["id"] = int(line[3:].strip())
        elif line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line[5:].strip())
        elif line == "" and current:
            events.append(current)
            current = {}
            if stop(events):
                return events
    return events


def _timeline_seqs(events: list[dict[str, Any]]) -> list[int]:
    return [e["id"] for e in events if e.get("event") == "timeline"]


def test_sse__full_replay_ordered_and_closes_on_terminal(client, auth_headers, llm):
    """跑完并批准 (任务到终态 completed) 后连 SSE: 全量按 seq 重放, 推完即关。"""
    llm(happy_script())
    alex = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "SSE 全量重放输入"}, headers=alex)
    task_id = r.json()["task_id"]
    snap = wait_status(client, alex, task_id, {"awaiting_approval"})
    approval_id = next(i for i in snap["timeline"]
                       if i["label"] == "request_approval")["detail"]["approval_id"]
    decided = client.post(f"/approvals/{approval_id}/decide",
                          json={"decision": "approved"}, headers=auth_headers(CHRIS))
    assert decided.status_code == 200, decided.text
    snap = wait_status(client, alex, task_id, {"completed"})
    expected_seqs = [i["seq"] for i in snap["timeline"]]

    with client.stream("GET", f"/agent-tasks/{task_id}/events", headers=alex) as resp:
        assert resp.status_code == 200
        events = read_sse(resp, stop=lambda evs: False)  # 读到服务端自己关流
    seqs = _timeline_seqs(events)
    assert seqs == expected_seqs  # 不重复不遗漏, 顺序唯一
    statuses = [e["data"]["status"] for e in events if e.get("event") == "status"]
    assert statuses[-1] == "completed"  # 终态推完即关 (读循环自然结束就是证据)


def test_sse__reconnect_with_last_event_id_no_dup_no_gap(sse_server, auth_headers, llm):
    """验收 18 的断线重连: 任务还在跑时连上真流, 中途断开, 带 Last-Event-ID
    重连, 从那个 seq **之后**接着推 —— 不重复不遗漏。"""
    import httpx

    llm(happy_script(), delay_s=0.25)
    alex = auth_headers(ALEX)
    with httpx.Client(base_url=sse_server, timeout=10) as hc:
        r = hc.post("/agent-tasks", json={"text": "SSE 断线重连输入"}, headers=alex)
        assert r.status_code == 201, r.text
        task_id = r.json()["task_id"]

        # 第一段: 任务还在跑时连上, 事件一条条冒出来, 收满 3 条 timeline 就"断线"
        with hc.stream("GET", f"/agent-tasks/{task_id}/events", headers=alex) as resp:
            assert resp.status_code == 200
            part1 = read_sse(resp, stop=lambda evs: len(_timeline_seqs(evs)) >= 3)
        part1_seqs = _timeline_seqs(part1)
        assert len(part1_seqs) == 3
        last_id = part1_seqs[-1]

        snap = wait_status(hc, alex, task_id, {"awaiting_approval"})
        all_seqs = [i["seq"] for i in snap["timeline"]]

        # 第二段: 浏览器重连会自动带 Last-Event-ID, 这里手工带上同一个头
        with hc.stream("GET", f"/agent-tasks/{task_id}/events", headers={
            **alex, "Last-Event-ID": str(last_id),
        }) as resp:
            part2 = read_sse(
                resp,
                stop=lambda evs: bool(_timeline_seqs(evs))
                and _timeline_seqs(evs)[-1] >= all_seqs[-1],
            )
        part2_seqs = _timeline_seqs(part2)

    assert min(part2_seqs) == last_id + 1  # 从断点之后接着推, 不重复
    assert part1_seqs + part2_seqs == all_seqs  # 合起来一条不漏, 顺序唯一
    print(f"\n[验收18] part1={part1_seqs} last_event_id={last_id} "
          f"part2={part2_seqs} full={all_seqs}")


def test_sse__404_and_401(client, auth_headers):
    assert client.get("/agent-tasks/999999/events",
                      headers=auth_headers(ALEX)).status_code == 404
    assert client.get("/agent-tasks/1/events").status_code == 401


# ===== 任务列表 (W4 收尾: GET /agent-tasks) =====
#
# 全部用 insert_task 裸 SQL 造数据, 不跑 Agent —— 列表接口是纯查询,
# 它的分支 (排序/过滤/上界/截断/两路策略名 join) 与状态机无关。


def test_list__open_tasks_first_then_time_desc(client, auth_headers):
    """未走完的排最前 (哪怕它最老), 其余按时间倒序。"""
    async def seed(conn):
        old_done = await insert_task(conn, input_hash="lh1", status="completed",
                                     input='{"text": "老的已完成"}')
        new_done = await insert_task(conn, input_hash="lh2", status="failed",
                                     input='{"text": "新的失败"}')
        waiting = await insert_task(conn, input_hash="lh3", status="awaiting_approval",
                                    input='{"text": "等审批"}')
        for task_id, hours in ((waiting, 3), (old_done, 2), (new_done, 1)):
            await conn.execute(
                "UPDATE agent_tasks SET created_at = now() - make_interval(hours => $2) "
                "WHERE id = $1", task_id, hours)
        return waiting, old_done, new_done

    waiting, old_done, new_done = db(seed)
    r = client.get("/agent-tasks", headers=auth_headers(ALEX))
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["tasks"]] == [waiting, new_done, old_done]


def test_list__status_filter_limit_bounds_and_permissions(
    client, auth_headers, viewer_headers
):
    async def seed(conn):
        await insert_task(conn, input_hash="lf1", status="awaiting_approval",
                          input='{"text": "等审批的"}')
        await insert_task(conn, input_hash="lf2", status="completed",
                          input='{"text": "已完成的"}')

    db(seed)
    headers = auth_headers(ALEX)
    only = client.get("/agent-tasks?status=awaiting_approval", headers=headers)
    assert [t["status"] for t in only.json()["tasks"]] == ["awaiting_approval"]
    # 写错的过滤值 422, 不静默返回空列表 (Literal 挡住)
    assert client.get("/agent-tasks?status=nonsense", headers=headers).status_code == 422

    # limit 上界 100、下界 1, 越界 422; limit=1 生效
    assert client.get("/agent-tasks?limit=0", headers=headers).status_code == 422
    assert client.get("/agent-tasks?limit=101", headers=headers).status_code == 422
    assert len(client.get("/agent-tasks?limit=1", headers=headers).json()["tasks"]) == 1

    # 权限与单条读取同档: viewer 能看, 未登录 401
    assert client.get("/agent-tasks", headers=viewer_headers).status_code == 200
    assert client.get("/agent-tasks").status_code == 401


def test_list__default_limit_is_20(client, auth_headers):
    async def seed(conn):
        for i in range(25):
            await insert_task(conn, input_hash=f"bulk-{i}", status="completed",
                              input='{"text": "批量造的行"}')

    db(seed)
    r = client.get("/agent-tasks", headers=auth_headers(ALEX))
    assert len(r.json()["tasks"]) == 20


def test_list__preview_truncated_and_names_joined_both_ways(client, auth_headers):
    """截断 80 字带标记; 策略名两路 join: 目标策略 与 任务自己建的草稿。"""
    long_text = "改" * 100

    async def seed(conn):
        policy_id = await conn.fetchval(
            "INSERT INTO policies (name) VALUES ('列表测试策略') RETURNING id")
        # 路一: 改已有策略的任务, 名字从 input.target_policy_id join 到
        target_task = await insert_task(
            conn, input_hash="ln1", status="clarifying",
            input=json.dumps({"text": long_text, "target_policy_id": policy_id},
                             ensure_ascii=False))
        # 路二: 新建策略的任务, 名字从它时间线上的草稿步骤反查 (LATERAL)
        version_id = await conn.fetchval(
            "INSERT INTO policy_versions (policy_id, version, body, status, source) "
            "VALUES ($1, 1, '{}', 'awaiting_approval', 'agent') RETURNING id",
            policy_id)
        draft_task = await insert_task(conn, input_hash="ln2",
                                       status="awaiting_approval",
                                       input='{"text": "短输入"}')
        await conn.execute(
            "INSERT INTO agent_steps (task_id, seq, tool_name, result_summary, status) "
            "VALUES ($1, 1, 'create_policy', $2::jsonb, 'ok')",
            draft_task, json.dumps({"version_id": version_id}))
        return target_task, draft_task

    target_task, draft_task = db(seed)
    r = client.get("/agent-tasks", headers=auth_headers(ALEX))
    tasks = {t["id"]: t for t in r.json()["tasks"]}

    t = tasks[target_task]
    assert len(t["input_preview"]) == 80 and t["input_truncated"] is True
    assert t["policy_name"] == "列表测试策略"
    assert t["requested_by"] == "Alex Chen"  # 种子 user 3 的显示名

    d = tasks[draft_task]
    assert d["input_preview"] == "短输入" and d["input_truncated"] is False
    assert d["policy_name"] == "列表测试策略"


# ===== 验收 16 + 变异 21 的钉子 =====


def test_acceptance16__operator_draft_reaches_approval_but_publish_403(
    client, auth_headers, llm
):
    """operator 跑 Agent -> 草案提交审批成功; 绕过前端直接调发布接口仍 403。
    (前端把发布按钮按角色置灰只是体验优化, 这条才是安全测试。)"""
    llm(happy_script())
    alex = auth_headers(ALEX)
    r = client.post("/agent-tasks", json={"text": "验收16 输入"}, headers=alex)
    snap = wait_status(client, alex, r.json()["task_id"], {"awaiting_approval"})
    version_id = next(i for i in snap["timeline"]
                      if i["label"] == "create_policy")["detail"]["version_id"]
    approval = next(i for i in snap["timeline"] if i["label"] == "request_approval")
    assert approval["detail"]["approval_id"]  # 提交审批这一步是成功的

    denied = client.post(f"/policy-versions/{version_id}/publish", headers=alex)
    assert denied.status_code == 403, denied.text


def test_mutation21_pin__route_gate_rejects_with_route_wording(client, auth_headers):
    """变异 21 的钉子。W3 经验: 拆掉路由门后 service 层第二道闸也 403, 状态码
    测不出区别。SPEC 预想的"不存在的版本号 -> 没门就先查库变 404"**实测不成立**:
    publish 的 service 闸在查版本之前就先做 RBAC, 拆门后照样 403。真正把两层
    分开的是 W3 就立下的话术差异 (policies.py _service_errors 的注释: "话术刻意
    与它不同 —— 测试靠话术区分拦截层"):
      有路由门: "当前角色无 ... 权限" (require_permission);
      拆掉之后: "该操作需要 manager 及以上角色" (service 层)。
    变异实测 (把 ManagerDep 换成 OperatorDep): 本条在话术断言上红, 详见第三段
    完成报告。"""
    denied = client.post("/policy-versions/99999999/publish",
                         headers=auth_headers(ALEX))
    assert denied.status_code == 403, denied.text
    # 路由门的话术; 变异后变成 service 层那句"该操作需要 manager 及以上角色"而红
    assert "权限" in denied.json()["detail"]

    # 对照: manager 过得了两道闸, 才碰到数据库的 404 —— 证明拒绝发生在查库之前
    as_manager = client.post("/policy-versions/99999999/publish",
                             headers=auth_headers(CHRIS))
    assert as_manager.status_code == 404
