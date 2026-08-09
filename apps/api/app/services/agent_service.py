"""Agent 任务的增删查、去重、租约与状态推进 (SPEC-002 第二、三、七节)。

设计要点:
1. **去重由数据库的部分唯一索引挡住** (agent_tasks_one_open): 同一个用户同一句话,
   最多只有一条还没走完 (running/clarifying) 的任务。应用层用
   INSERT ... ON CONFLICT 撞它, 撞上就返回已有任务, 不猜时间窗、不信前端传 id;
2. **租约与栅栏**: 判死靠 heartbeat_at 的时间差 (不靠"启动"这个事件); 防"假死
   复活"的闸是把"确认归属"和"写"合并成同一条 SQL —— 所有运行期写库都带
   `AND status = 'running' AND runner_id = :runner`, 受影响 0 行即 LeaseLost,
   当场停手一个字都不写。不写成"先 SELECT 确认再 UPDATE": 查完到写之间那一瞬间
   情况可能已经变了;
3. **两张表一条编号**: agent_steps 与 agent_clarifications 共用 agent_tasks.next_seq
   发号。发号本身就是那道闸 (`SET next_seq = next_seq + 1 ... WHERE ... runner_id
   = :runner RETURNING next_seq`), 且与写记录同一个事务 —— 闸拦下时记录跟着回滚;
4. **LLM 调用计数持久在 ai_usage 里** (每次调用落一行, 打桩也落): "单任务调用总数
   ≤12 跨轮累加不重置"必须活过进程重启 —— clarifying 的任务可能由重启后的新进程
   接着跑, 内存里的计数器带不过去。

status 取值备注: agent_tasks.status 的 CHECK 里有 'rejected', **本 SPEC 用不到**
(审批被拒是策略版本的事, 不是任务的事 —— 批准和否决都把任务推进到 completed)。
0001 建表时留下的, 不删也不用, 免得下一个人以为漏实现了 (SPEC-002 第四节)。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .llm_client import LLMResponse


class TaskNotFound(Exception):
    """任务不存在 -> 404。"""


class NotTaskOwner(Exception):
    """回答澄清的不是发起人本人 -> 403 (SPEC-002 第三节: 草案的 created_by 记的是
    发起人, 如果 A 的任务能被 B 回答, "这条策略到底是谁的意思"就说不清了)。"""


class TransitionConflict(Exception):
    """当前状态不允许该流转 (并发回答撞车等) -> 409。"""

    def __init__(self, current_status: str) -> None:
        super().__init__(current_status)
        self.current_status = current_status


class LeaseLost(Exception):
    """那道闸: 写库时发现租约已被收走 (被判死后有人重开了任务, 或清扫先到了)。

    拿到它的运行循环必须当场停手 —— 事务回滚, 本次想写的东西一个字都不落库。
    """

    def __init__(self, task_id: int) -> None:
        super().__init__(task_id)
        self.task_id = task_id


# ===== 输入归一化与去重 =====

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_input(input_text: str, target_policy_id: int | None) -> tuple[str, str]:
    """归一化 + 哈希, 返回 (归一化文本, input_hash)。

    归一化 = 去首尾空白 + 压缩连续空白为单个空格 + **拼上目标 policy_id**
    (新建时为 0), 然后取 sha256。目标策略必须进哈希: 同一句"把冷却改成 600 秒"
    用在两条不同策略上是两件事, 只哈希文本会误挡 (SPEC-002 第七节)。
    """
    normalized = _WHITESPACE_RE.sub(" ", input_text.strip())
    digest = hashlib.sha256(
        f"{target_policy_id or 0}\n{normalized}".encode()
    ).hexdigest()
    return normalized, digest


# ON CONFLICT 的冲突目标写成部分唯一索引的列 + 谓词, 精确撞 agent_tasks_one_open。
# heartbeat_at 新建即盖第一次戳: **任何让任务变成 running 的写操作都同时打一次卡**
# (SPEC-002 第二节写死的规矩), 判死因此只需比对 heartbeat_at 一个起算点 ——
# 拿 created_at 当兜底起算点会让"老任务被人回答后刚复活"零宽限期, 出过事。
_INSERT_TASK = text("""
    INSERT INTO agent_tasks (user_id, task_type, input, input_hash, heartbeat_at)
    VALUES (:user_id, :task_type, CAST(:input AS jsonb), :input_hash, now())
    ON CONFLICT (user_id, input_hash)
        WHERE status IN ('running', 'clarifying')
        DO NOTHING
    RETURNING id
""")

_FIND_OPEN_TASK = text("""
    SELECT id, status, stage, created_at, heartbeat_at,
           heartbeat_at IS NULL
               OR heartbeat_at < now() - make_interval(secs => :lease_timeout)
               AS heartbeat_stale
    FROM agent_tasks
    WHERE user_id = :user_id AND input_hash = :input_hash
      AND status IN ('running', 'clarifying')
""")

_GET_TASK = text("""
    SELECT id, user_id, task_type, input, stage, status, error_code, error_detail,
           input_hash, runner_id, heartbeat_at, next_seq, created_at, completed_at
    FROM agent_tasks WHERE id = :id
""")


async def create_task(
    session: AsyncSession,
    *,
    user_id: int,
    input_text: str,
    target_policy_id: int | None = None,
    task_type: str = "policy_compile",
    lease_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """开任务, 重复提交由数据库挡住。返回:

    - created=True: 新任务;
    - created=False: 撞上了还没走完的同一句话 —— 返回那一条。existing['status']
      是 clarifying 时界面该引导人去回答; suspected_interrupted=True 时
      (running 但打卡已停, 还没到判死线) 界面标"疑似中断, 稍后可重试",
      **不能报"重复提交"** —— 用户明明什么都没等到 (SPEC-002 第二节的代价一栏)。
    """
    normalized, input_hash = normalize_input(input_text, target_policy_id)
    payload = json.dumps(
        {"text": input_text, "normalized": normalized,
         "target_policy_id": target_policy_id},
        ensure_ascii=False,
    )
    params = {
        "user_id": user_id, "task_type": task_type,
        "input": payload, "input_hash": input_hash,
    }
    # 撞索引后老任务恰好在同一瞬间走完的窄竞态: SELECT 落空就重插一次
    for _ in range(2):
        task_id = (await session.execute(_INSERT_TASK, params)).scalar_one_or_none()
        if task_id is not None:
            return {"task_id": task_id, "created": True, "suspected_interrupted": False}
        existing = (
            await session.execute(
                _FIND_OPEN_TASK,
                {"user_id": user_id, "input_hash": input_hash,
                 "lease_timeout": lease_timeout_seconds},
            )
        ).mappings().one_or_none()
        if existing is not None:
            return {
                "task_id": existing["id"],
                "created": False,
                "status": existing["status"],
                "stage": existing["stage"],
                "suspected_interrupted": bool(
                    existing["status"] == "running" and existing["heartbeat_stale"]
                ),
            }
    raise RuntimeError("去重索引与查询交替落空两次, 不该发生")  # pragma: no cover


async def get_task(session: AsyncSession, task_id: int) -> dict[str, Any]:
    row = (await session.execute(_GET_TASK, {"id": task_id})).mappings().one_or_none()
    if row is None:
        raise TaskNotFound
    return dict(row)


# ===== 时间线 (Trace 的事实源: 两张表按同一条 seq 排成一条线) =====

# 第三段补列 (只动 SELECT, 不动任何写路径): Trace UI 按 SPEC-002 第十节要给人看
# 工具名、参数、耗时、重试、token 数, 还要显示 transition 去了哪个 stage
# (在 arguments 里) —— 第一段只选了四列, 是接口没预料到 UI 的需求, 不是行为变化。
# 澄清行没有这些概念, 一律 NULL。
_TIMELINE = text("""
    SELECT seq, kind, label, detail, arguments,
           latency_ms, retry_count, input_tokens, output_tokens
    FROM (
        SELECT s.seq AS seq,
               CASE WHEN s.tool_name = 'stage_transition'
                    THEN 'transition' ELSE 'step' END AS kind,
               s.tool_name AS label,
               s.result_summary AS detail,
               s.arguments AS arguments,
               s.latency_ms AS latency_ms,
               s.retry_count AS retry_count,
               s.input_tokens AS input_tokens,
               s.output_tokens AS output_tokens
        FROM agent_steps s WHERE s.task_id = :task_id
        UNION ALL
        SELECT c.asked_seq, 'clarification_question', c.question,
               NULL, NULL, NULL, NULL, NULL, NULL
        FROM agent_clarifications c WHERE c.task_id = :task_id
        UNION ALL
        SELECT c.answered_seq, 'clarification_answer', c.answer,
               NULL, NULL, NULL, NULL, NULL, NULL
        FROM agent_clarifications c
        WHERE c.task_id = :task_id AND c.answered_seq IS NOT NULL
    ) t ORDER BY seq
""")


async def get_timeline(session: AsyncSession, task_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(_TIMELINE, {"task_id": task_id})).mappings().all()
    return [dict(r) for r in rows]


# ===== 任务列表 (W4 收尾: 审批人打开系统要看见"有几条等我批") =====

# 列表里输入文本的截断长度。整段原文在单条接口 (GET /agent-tasks/{id}) 里。
TASK_LIST_PREVIEW_CHARS = 80

# 一条 SQL 出结果, 不 N+1:
# - 发起人显示名直接 join users (没有 display_name 的账号退回 email);
# - 关联策略名分两路: 改已有策略的任务从 input.target_policy_id 找;
#   新建策略的任务从它自己在时间线上留下的草稿步骤反查 (LATERAL 每行一次
#   索引查, 与 _TASK_DRAFT_VERSION 同一个判据), 编译失败没建出草稿时为 NULL。
#   两路都有值时取草稿那路 —— 它是任务实际动到的那条策略。
# 排序: 未走完的 (running/clarifying/awaiting_approval) 在前, 组内时间倒序。
_LIST_TASKS = text(f"""
    SELECT t.id, t.status, t.stage, t.error_code,
           left(t.input ->> 'text', {TASK_LIST_PREVIEW_CHARS}) AS input_preview,
           length(t.input ->> 'text') > {TASK_LIST_PREVIEW_CHARS} AS input_truncated,
           COALESCE(u.display_name, u.email) AS requested_by,
           COALESCE(draft.name, target.name) AS policy_name,
           t.created_at, t.completed_at
    FROM agent_tasks t
    JOIN users u ON u.id = t.user_id
    LEFT JOIN policies target
           ON target.id = CAST(t.input ->> 'target_policy_id' AS bigint)
    LEFT JOIN LATERAL (
        SELECT p.name
        FROM agent_steps s
        JOIN policy_versions pv
             ON pv.id = CAST(s.result_summary ->> 'version_id' AS bigint)
        JOIN policies p ON p.id = pv.policy_id
        WHERE s.task_id = t.id
          AND s.tool_name IN ('create_policy', 'add_policy_version')
          AND s.result_summary ? 'version_id'
        ORDER BY s.seq DESC LIMIT 1
    ) draft ON true
    WHERE CAST(:status AS text) IS NULL OR t.status = :status
    ORDER BY (t.status IN ('running', 'clarifying', 'awaiting_approval')) DESC,
             t.created_at DESC, t.id DESC
    LIMIT :limit
""")


async def list_tasks(
    session: AsyncSession, *, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """任务列表。limit 的默认值与硬上限 (20/100) 由路由层的 Query 声明把住,
    这里只透传 —— 上界是响应体的规矩 (SPEC-005 决策 4), 不是查询的业务逻辑。"""
    rows = (
        await session.execute(_LIST_TASKS, {"status": status, "limit": limit})
    ).mappings().all()
    return [dict(r) for r in rows]


# ===== 租约: 认领、打卡、发号闸、清扫 =====

_CLAIM = text("""
    UPDATE agent_tasks SET runner_id = :runner, heartbeat_at = now()
    WHERE id = :id AND status = 'running' AND runner_id IS NULL
    RETURNING id
""")

# 发号与归属确认是同一条 SQL: 拿得到号 = 我还是主人。0 行即租约已被收走。
_ALLOC_SEQ = text("""
    UPDATE agent_tasks SET next_seq = next_seq + 1
    WHERE id = :id AND status = 'running' AND runner_id = :runner
    RETURNING next_seq
""")

_BEAT = text("""
    UPDATE agent_tasks SET heartbeat_at = now()
    WHERE status = 'running' AND runner_id = :runner
    RETURNING id
""")

# 判死靠时间差, 不靠"启动"这个事件, 且**只比对 heartbeat_at 一个起算点**
# (SPEC-002 第二节): running 的不变量是"必有心跳戳" —— 新建、认领、打卡、
# 人回答恢复, 每一处都盖 now()。所以:
# - 戳过期 = 失联 (含"建完就死、从没人认领"的场景, 建任务时盖的戳照样会过期);
# - 戳为 NULL = 不变量已破 (只能来自代码回归或手改库), 视为立即失联。
# 不看 created_at: 拿它当兜底起算点, 老任务被人回答刚复活就满足判死条件,
# 零宽限期, 第一段实测出过事 (SPEC-002 第二节的复现记录)。
# 两种判死的 error_detail 必须分开 (SPEC-002 第二节): NULL 不是"服务重启",
# 是"有人写了一处让任务处于 running 却忘了盖戳" —— 沿用失联那句话, 排查的人
# 会去追一个根本没发生过的重启。
_REAP_STALE = text("""
    UPDATE agent_tasks
       SET status = 'dead_letter',
           error_code = 'lease_timeout',
           error_detail = '任务失联, 可能是服务重启或进程异常',
           completed_at = now()
     WHERE status = 'running'
       AND heartbeat_at < now() - make_interval(secs => :timeout)
    RETURNING id
""")

_REAP_NULL_HEARTBEAT = text("""
    UPDATE agent_tasks
       SET status = 'dead_letter',
           error_code = 'lease_timeout',
           error_detail = '心跳戳缺失: running 任务必有心跳戳的不变量已破 '
                          '(某处让任务进入 running 却没盖戳, 或手改库), '
                          '见 SPEC-002 第二节',
           completed_at = now()
     WHERE status = 'running' AND heartbeat_at IS NULL
    RETURNING id
""")

# clarifying 不打卡 (没有进程在跑它), 由生存期上限收尾: 未回答的问题挂了超过
# TTL 即死信。判据用 asked_at —— 进入 clarifying 的那一刻必然问了一个问题。
_REAP_CLARIFY_TTL = text("""
    UPDATE agent_tasks t
       SET status = 'dead_letter',
           error_code = 'clarify_timeout',
           error_detail = '等待澄清超时: 问题挂了超过生存期没人回答',
           completed_at = now()
     WHERE t.status = 'clarifying'
       AND EXISTS (
           SELECT 1 FROM agent_clarifications c
           WHERE c.task_id = t.id AND c.answer IS NULL
             AND c.asked_at < now() - make_interval(hours => :ttl_hours)
       )
    RETURNING t.id
""")


async def claim_task(session: AsyncSession, task_id: int, runner_id: str) -> bool:
    """认领一条 running 且无主的任务。False = 已被别人认领或状态已变。"""
    claimed = (
        await session.execute(_CLAIM, {"id": task_id, "runner": runner_id})
    ).scalar_one_or_none()
    return claimed is not None


async def allocate_seq(session: AsyncSession, task_id: int, runner_id: str) -> int:
    """从任务的 next_seq 上原子取号。这就是那道闸: 0 行即 LeaseLost。"""
    seq = (
        await session.execute(_ALLOC_SEQ, {"id": task_id, "runner": runner_id})
    ).scalar_one_or_none()
    if seq is None:
        raise LeaseLost(task_id)
    return int(seq)


async def beat(session: AsyncSession, runner_id: str) -> int:
    """给本进程名下所有在跑任务打一次卡, 返回打到卡的任务数。"""
    rows = (await session.execute(_BEAT, {"runner": runner_id})).scalars().all()
    return len(rows)


async def reap(
    session: AsyncSession, *, lease_timeout_seconds: int, task_ttl_hours: int
) -> list[dict[str, Any]]:
    """清扫: 失联判死 + 澄清超时判死。返回 [{task_id, reason}], 由调用方
    (agent_runtime, 它能 import policy_service) 把死信任务的草稿标 discarded。"""
    stale = (
        await session.execute(_REAP_STALE, {"timeout": lease_timeout_seconds})
    ).scalars().all()
    broken = (await session.execute(_REAP_NULL_HEARTBEAT)).scalars().all()
    expired = (
        await session.execute(_REAP_CLARIFY_TTL, {"ttl_hours": task_ttl_hours})
    ).scalars().all()
    return [
        *({"task_id": int(t), "reason": "lease_timeout"} for t in [*stale, *broken]),
        *({"task_id": int(t), "reason": "clarify_timeout"} for t in expired),
    ]


# ===== 步骤与状态推进 (全部过闸) =====

_INSERT_STEP = text("""
    INSERT INTO agent_steps (task_id, seq, tool_name, arguments, result_summary,
                             status, latency_ms, retry_count, input_tokens, output_tokens)
    VALUES (:task_id, :seq, :tool_name, CAST(:arguments AS jsonb),
            CAST(:result_summary AS jsonb), :status, :latency_ms, :retry_count,
            :input_tokens, :output_tokens)
""")

_SET_STAGE = text("""
    UPDATE agent_tasks SET stage = :stage
    WHERE id = :id AND status = 'running' AND runner_id = :runner
    RETURNING id
""")

_FINISH = text("""
    UPDATE agent_tasks
       SET status = :status, stage = :stage, error_code = :error_code,
           error_detail = :error_detail,
           completed_at = CASE WHEN :terminal THEN now() ELSE NULL END,
           runner_id = NULL, heartbeat_at = NULL
     WHERE id = :id AND status = 'running' AND runner_id = :runner
    RETURNING id
""")


async def record_step(
    session: AsyncSession,
    task_id: int,
    runner_id: str,
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    status: str = "ok",
    latency_ms: int | None = None,
    retry_count: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> int:
    """发号 + 落一条步骤, 同一个事务。闸拦下时抛 LeaseLost, 整个事务回滚 ——
    与这条步骤同事务的工具副作用 (比如刚插的草稿行) 一并消失, 这正是验收 15
    要的"不产生任何写入"。返回该步骤拿到的 seq。"""
    seq = await allocate_seq(session, task_id, runner_id)
    await session.execute(_INSERT_STEP, {
        "task_id": task_id, "seq": seq, "tool_name": tool_name,
        "arguments": json.dumps(arguments, ensure_ascii=False)
        if arguments is not None else None,
        "result_summary": json.dumps(result_summary, ensure_ascii=False)
        if result_summary is not None else None,
        "status": status, "latency_ms": latency_ms, "retry_count": retry_count,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
    })
    return seq


async def advance_stage(
    session: AsyncSession, task_id: int, runner_id: str, stage: str
) -> None:
    """推进状态机的 stage 并在时间线上留一条 (每次状态迁移写一条 agent_steps,
    SPEC-002 第四节)。"""
    await record_step(
        session, task_id, runner_id,
        tool_name="stage_transition", arguments={"to": stage},
    )
    moved = (
        await session.execute(
            _SET_STAGE, {"id": task_id, "runner": runner_id, "stage": stage}
        )
    ).scalar_one_or_none()
    if moved is None:
        raise LeaseLost(task_id)


async def finish_task(
    session: AsyncSession,
    task_id: int,
    runner_id: str,
    *,
    status: str,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    """running -> awaiting_approval / failed / dead_letter, 过闸。

    先落时间线再翻状态 (发号闸只对 running 放行, 顺序反了最后一步就记不上)。
    """
    terminal = status != "awaiting_approval"
    await record_step(
        session, task_id, runner_id,
        tool_name="stage_transition",
        arguments={"to": status, "error_code": error_code},
        result_summary={"error_detail": error_detail} if error_detail else None,
        status="ok" if not terminal or error_code is None else "error",
    )
    moved = (
        await session.execute(_FINISH, {
            "id": task_id, "runner": runner_id, "status": status, "stage": status,
            "error_code": error_code, "error_detail": error_detail,
            "terminal": terminal,
        })
    ).scalar_one_or_none()
    if moved is None:
        raise LeaseLost(task_id)


_COMPLETE = text("""
    UPDATE agent_tasks
       SET status = 'completed', stage = 'completed',
           completed_at = now(), next_seq = next_seq + 1
     WHERE id = :id AND status = 'awaiting_approval'
    RETURNING next_seq
""")


async def complete_task(session: AsyncSession, task_id: int, decision: str) -> bool:
    """awaiting_approval -> completed, 由 policy_service.decide_approval 回写调用
    (SPEC-002 第四节: 批准和否决都推进)。任务不在 awaiting_approval (比如已死信)
    时静默返回 False —— 审批本身不该因此失败。无 runner 闸: 等审批的任务没有主人,
    条件更新本身就是并发保护。"""
    seq = (
        await session.execute(_COMPLETE, {"id": task_id})
    ).scalar_one_or_none()
    if seq is None:
        return False
    await session.execute(_INSERT_STEP, {
        "task_id": task_id, "seq": int(seq), "tool_name": "stage_transition",
        "arguments": json.dumps({"to": "completed", "decision": decision},
                                ensure_ascii=False),
        "result_summary": None, "status": "ok", "latency_ms": None,
        "retry_count": 0, "input_tokens": None, "output_tokens": None,
    })
    return True


# ===== 澄清 (多轮, SPEC-002 第三节) =====

_INSERT_CLARIFICATION = text("""
    INSERT INTO agent_clarifications (task_id, asked_seq, question)
    VALUES (:task_id, :seq, :question)
    RETURNING id
""")

_TO_CLARIFYING = text("""
    UPDATE agent_tasks
       SET status = 'clarifying', stage = 'clarifying',
           runner_id = NULL, heartbeat_at = NULL
     WHERE id = :id AND status = 'running' AND runner_id = :runner
    RETURNING id
""")

# 回答的并发闸: 发号 + clarifying -> running 合成一条, 两个人同时回答只有一个成功。
# runner_id 置 NULL: 原 runner 早在进 clarifying 时就交出了租约, 恢复执行的进程
# 要重新 claim。heartbeat_at 盖 now(): 变回 running 就是一次打卡 (SPEC-002 第二节),
# 宽限期从回答这一刻重新起算 —— 置 NULL 或留旧值都会让下一次清扫误杀刚复活的任务。
_ANSWER_RESUME = text("""
    UPDATE agent_tasks
       SET next_seq = next_seq + 1, status = 'running', stage = 'discovering',
           runner_id = NULL, heartbeat_at = now()
     WHERE id = :id AND status = 'clarifying'
    RETURNING next_seq
""")

_FILL_ANSWER = text("""
    UPDATE agent_clarifications
       SET answer = :answer, answered_by = :user_id, answered_seq = :seq,
           answered_at = now()
     WHERE task_id = :task_id AND answer IS NULL
    RETURNING id
""")

_COUNT_CLARIFICATIONS = text(
    "SELECT count(*) FROM agent_clarifications WHERE task_id = :task_id"
)


async def ask_clarification(
    session: AsyncSession, task_id: int, runner_id: str, question: str
) -> dict[str, Any]:
    """把问题抛回给人: 发号、落一行澄清、任务转 clarifying 并交出租约。
    "一个任务同时最多一个未回答的问题"由 agent_clarifications_one_pending 保证,
    这里不写并发判断 (SPEC-002 第三节)。"""
    seq = await allocate_seq(session, task_id, runner_id)
    clarification_id = (
        await session.execute(
            _INSERT_CLARIFICATION,
            {"task_id": task_id, "seq": seq, "question": question},
        )
    ).scalar_one()
    moved = (
        await session.execute(_TO_CLARIFYING, {"id": task_id, "runner": runner_id})
    ).scalar_one_or_none()
    if moved is None:
        raise LeaseLost(task_id)
    return {"clarification_id": clarification_id, "asked_seq": seq}


async def answer_clarification(
    session: AsyncSession, task_id: int, user_id: int, answer: str
) -> dict[str, Any]:
    """人回答澄清: 只有发起人本人能答; 答完任务回 running、stage 回 discovering
    (人的回答很可能提到新的东西, 重捞一遍区/传感器/角色, 毫秒级, SPEC-002 第三节)。
    """
    task = (await session.execute(_GET_TASK, {"id": task_id})).mappings().one_or_none()
    if task is None:
        raise TaskNotFound
    if task["user_id"] != user_id:
        raise NotTaskOwner
    seq = (
        await session.execute(_ANSWER_RESUME, {"id": task_id})
    ).scalar_one_or_none()
    if seq is None:  # 并发回答撞车, 或任务已死信/已走完
        raise TransitionConflict(task["status"])
    filled = (
        await session.execute(_FILL_ANSWER, {
            "task_id": task_id, "answer": answer, "user_id": user_id, "seq": int(seq),
        })
    ).scalar_one_or_none()
    if filled is None:  # clarifying 却没有未回答的问题: 状态被绕过应用层改坏了
        raise TransitionConflict(task["status"])
    return {"task_id": task_id, "answered_seq": int(seq)}


async def clarify_rounds_used(session: AsyncSession, task_id: int) -> int:
    """已用掉的澄清轮次 = 该任务名下的澄清行数 (问一次算一轮)。"""
    return int(
        (await session.execute(_COUNT_CLARIFICATIONS, {"task_id": task_id})).scalar_one()
    )


# ===== LLM 计量 (SPEC-002 第九节: 每次调用落 ai_usage) =====

_INSERT_USAGE = text("""
    INSERT INTO ai_usage (task_id, model, prompt_version, input_tokens, output_tokens,
                          estimated_cost_usd, latency_ms, cache_hit)
    VALUES (:task_id, :model, :prompt_version, :input_tokens, :output_tokens,
            :cost, :latency_ms, :cache_hit)
""")

_COUNT_USAGE = text("SELECT count(*) FROM ai_usage WHERE task_id = :task_id")


async def record_llm_usage(
    session: AsyncSession, task_id: int, response: LLMResponse
) -> None:
    await session.execute(_INSERT_USAGE, {
        "task_id": task_id, "model": response.model,
        "prompt_version": response.prompt_version,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        # 客户端按 config 单价估的成本, 人民币元 (列名叫 usd 是已知债, 改名迁移
        # 留给 W5, SPEC-002 第九节末)。打桩与回放命中都是 0。
        "cost": response.estimated_cost_usd,
        "latency_ms": response.latency_ms, "cache_hit": response.cache_hit,
    })


async def llm_calls_used(session: AsyncSession, task_id: int) -> int:
    """已用掉的 LLM 调用数。数 ai_usage 的行而不是内存计数器: ≤12 这条硬上限
    跨轮累加不重置, 必须活过进程重启 (clarifying 的任务可能换个进程接着跑)。"""
    return int(
        (await session.execute(_COUNT_USAGE, {"task_id": task_id})).scalar_one()
    )


# ===== 草稿归属 (清扫与失败路径要把死掉任务的草稿标 discarded) =====

_TASK_DRAFT_VERSION = text("""
    SELECT CAST(result_summary ->> 'version_id' AS bigint)
    FROM agent_steps
    WHERE task_id = :task_id
      AND tool_name IN ('create_policy', 'add_policy_version')
      AND result_summary ? 'version_id'
    ORDER BY seq DESC LIMIT 1
""")


async def find_task_draft_version(session: AsyncSession, task_id: int) -> int | None:
    """从时间线里找回该任务新建的那一版草稿 (每个任务只新建一版, SPEC-002 第六节)。

    discard 本身在 policy_service; 串起两者的是 agent_runtime ——
    本模块不 import policy_service (它反向 import 本模块, 见 decide_approval)。
    """
    version_id = (
        await session.execute(_TASK_DRAFT_VERSION, {"task_id": task_id})
    ).scalar_one_or_none()
    return int(version_id) if version_id is not None else None
