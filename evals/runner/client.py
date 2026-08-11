"""HTTP 客户端 + 并发编排 + 成本护栏。

三条硬规矩 (SPEC-007 第六、七节, prompt 易错点 8/9/12/14):
- 建任务与回答**只走 HTTP** —— 绕过 HTTP 就绕过了权限层、去重层、并发预留槽位,
  量出来的不是交付的那条路;
- 429 不是用例失败: runner 自限并发到服务端上界, 撞上仍退避重试;
- 花费护栏在这层执勤: 每条用例结束即记账, 超过上限**当场不再发起新用例**,
  在跑的 (至多并发度条) 让它们收尾 —— 已完成部分照常归档, 不丢弃。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

TERMINAL_STATUSES = frozenset(
    {"awaiting_approval", "completed", "failed", "dead_letter"}
)
_MAX_ANSWER_ROUNDS = 3  # 服务端 agent_max_clarify_rounds 同值; 双保险防绕圈
_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0)

# 用例没给这个槽位的答案时的兜底话 (数据集 v1.3)。**集中定义一处**, 不许每条用例
# 各写各的 —— 兜底话散进 100 条用例里就是 100 份会走散的事实。
_FALLBACK_ANSWERS = {"capability_gap": "这个系统做不到"}
_FALLBACK_DEFAULT = "这条你按合理默认来"


def compose_answer(
    clarify_answer: dict[str, str], missing_slots: list[str]
) -> str:
    """按模型这一轮报的槽位挑出对应几条拼成回答 —— **模型问什么答什么**。

    v1.2 之前这里是一段死文本, 每一轮都把同一段话再念一遍。而模型每一轮问的槽位
    **不一样**: 从第二轮起它问的东西根本没被回答, 于是一进第二轮就必然耗尽三轮然后死
    (L2 19 条 / C1 17 条, 无一存活)。真人不会对不同的问题念同一段稿子。

    槽位顺序按模型问的顺序, 不按用例里的字典顺序 —— 答案的排列跟着问题走才像人在答。
    """
    if not missing_slots:
        # 读不到槽位时退回"把知道的都说一遍": 比不回答强, 但它是降级路径,
        # 调用方会把它记进 observations, 不许静默发生。
        return "; ".join(clarify_answer.values())
    parts = [
        clarify_answer.get(slot)
        or _FALLBACK_ANSWERS.get(slot, _FALLBACK_DEFAULT)
        for slot in missing_slots
    ]
    seen: dict[str, None] = {}  # 同一段文本挂在多个槽位下时只说一遍, 保序去重
    for p in parts:
        seen.setdefault(p, None)
    return "; ".join(seen)


class BudgetExceeded(Exception):
    """累计花费超过 --max-cost-cny。调度层收到后停发新用例。"""


class CostLedger:
    """累计花费台账 (人民币元, 按 ai_usage.estimated_cost_cny 逐任务入账)。"""

    def __init__(self, limit_cny: float | None) -> None:
        self.limit_cny = limit_cny
        self.total_cny = 0.0

    def add(self, cost_cny: float) -> None:
        self.total_cny += cost_cny

    @property
    def exceeded(self) -> bool:
        return self.limit_cny is not None and self.total_cny > self.limit_cny


class EvalApiClient:
    """评测账号的 HTTP 通道。全程同一个账号 —— 澄清"只有发起人能回答"。"""

    def __init__(
        self, base_url: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=30.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self, email: str, password: str) -> None:
        resp = await self._client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        resp.raise_for_status()
        token = self._client.cookies.get("sentinel_token")
        if not token:
            raise RuntimeError("登录没拿到会话 cookie")
        self._client.cookies.clear()
        self._client.headers["Authorization"] = f"Bearer {token}"

    async def _post_with_backoff(
        self, url: str, payload: dict[str, Any]
    ) -> httpx.Response:
        """429 退避重试 (易错点 9): 上界是服务端并发槽位, 撞上等一等就有。"""
        for backoff in (*_BACKOFF_S, None):
            resp = await self._client.post(url, json=payload)
            if resp.status_code != 429 or backoff is None:
                return resp
            await asyncio.sleep(backoff)
        raise AssertionError("unreachable")  # pragma: no cover

    async def create_task(self, text: str, *, poll_s: float = 0.5) -> int:
        """POST /agent-tasks 直到拿到**新建**的任务。

        created=False = 撞上同一句话还没走完的任务 (数据集里有一对刻意同文的
        用例): 等那条离开 open 状态再重试 —— 静默共用一条任务是两条用例量同一
        次运行, 且没有任何东西会报错 (SPEC-007 第七节点名的坑)。
        """
        while True:
            resp = await self._post_with_backoff("/agent-tasks", {"text": text})
            resp.raise_for_status()
            data = resp.json()
            if data["created"]:
                return int(data["task_id"])
            existing = int(data["task_id"])
            while (await self.get_task(existing))["status"] in (
                "running", "clarifying"
            ):
                await asyncio.sleep(poll_s)

    async def reply(self, task_id: int, answer: str) -> bool:
        """回答澄清。409 (已被回答/状态已变) 返回 False, 由轮询自然收敛。"""
        resp = await self._post_with_backoff(
            f"/agent-tasks/{task_id}/reply", {"answer": answer}
        )
        if resp.status_code == 409:
            return False
        resp.raise_for_status()
        return True

    async def get_task(self, task_id: int) -> dict[str, Any]:
        resp = await self._client.get(f"/agent-tasks/{task_id}")
        resp.raise_for_status()
        task: dict[str, Any] = resp.json()["task"]
        return task


async def drive_case(
    client: EvalApiClient,
    case: dict[str, Any],
    *,
    poll_s: float,
    max_wall_s: float,
    read_missing_slots: Callable[[int], Awaitable[list[str]]] | None = None,
) -> dict[str, Any]:
    """一条用例的完整生命周期: 建任务 -> 轮询 -> (clarify 类自动回答) -> 终局。

    返回 {task_id, final_status, answered_rounds, runner_timeout, blind_answers}。
    没有冻结回答的用例停在 clarifying 即终局 (模型问了但用例没打算答 ——
    reject/capability_gap 类这正是成功形态, 判分侧自会定夺)。

    `read_missing_slots` 读这条任务**当前那一问**报的槽位 (数据集 v1.3: 按槽位应答)。
    它读的是评测库而不是 HTTP —— `GET /agent-tasks/{id}` 的时间线里澄清行
    `arguments` 恒为 NULL, 服务端没有把结构化槽位暴露给读接口。**这是对
    "runner 只走 HTTP" 的一处让步, 照实记**: 那条规矩的理由是"绕过 HTTP 就绕过了
    权限层、去重层、并发预留槽位", 三者都在**写**路径上; 建任务与回答仍然只走 HTTP,
    这里只是读一列已经落库的槽位 (判分侧本来就从同一张表读它)。
    """
    clarify_answer = case.get("expected", {}).get("clarify_answer")
    task_id = await client.create_task(str(case["input"]))
    answered = 0
    blind = 0  # 读不到槽位、退回"把知道的都说一遍"的次数 —— 降级路径要留痕
    deadline = asyncio.get_running_loop().time() + max_wall_s
    while True:
        task = await client.get_task(task_id)
        status = str(task["status"])
        if status in TERMINAL_STATUSES:
            return {"task_id": task_id, "final_status": status,
                    "answered_rounds": answered, "runner_timeout": False,
                    "blind_answers": blind}
        if status == "clarifying":
            if clarify_answer is None or answered >= _MAX_ANSWER_ROUNDS:
                return {"task_id": task_id, "final_status": status,
                        "answered_rounds": answered, "runner_timeout": False,
                        "blind_answers": blind}
            slots = await read_missing_slots(task_id) if read_missing_slots else []
            if not slots:
                blind += 1
            answer = (
                compose_answer(clarify_answer, slots)
                if isinstance(clarify_answer, dict)
                else str(clarify_answer)  # v1.2 及更早的死文本形态, lint 已禁止
            )
            if await client.reply(task_id, answer):
                answered += 1
        if asyncio.get_running_loop().time() > deadline:
            # 兜底: 服务端预算与清扫应保证终局, 走到这说明流水线自己有问题
            return {"task_id": task_id, "final_status": status,
                    "answered_rounds": answered, "runner_timeout": True,
                    "blind_answers": blind}
        await asyncio.sleep(poll_s)


async def run_cases(
    cases: list[dict[str, Any]],
    run_one: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    concurrency: int,
    ledger: CostLedger,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """并发编排 + 成本护栏。返回 (完成结果 by case_id, 未跑的 case_id, 报错的 case_id)。

    run_one 完成一条用例并**已把该用例的花费入账** (cli 里它跑完即提取计量)。
    超限语义 (验收 19): 停发新用例, 在跑的让它们收尾, 已完成部分照常归档 ——
    超出量有上界 (至多并发度 × 单用例花费), 不是跑完再看账单。
    """
    sem = asyncio.Semaphore(concurrency)
    skipped: list[str] = []
    errored: list[str] = []
    results: dict[str, dict[str, Any]] = {}

    async def one(case: dict[str, Any]) -> None:
        case_id = str(case["id"])
        async with sem:
            if ledger.exceeded:
                skipped.append(case_id)
                return
            try:
                results[case_id] = await run_one(case)
            except BudgetExceeded:
                skipped.append(case_id)
            except Exception as e:  # 单条用例炸掉不拖垮整臂, 但要记名
                errored.append(case_id)
                print(f"  !! 用例 {case_id} 运行异常: {type(e).__name__}: {e}")

    await asyncio.gather(*(one(c) for c in cases))
    return results, sorted(skipped), sorted(errored)
