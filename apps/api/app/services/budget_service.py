"""花钱护栏: 预扣 -> 调用 -> 回补, 约束在数据库上 (SPEC-009 第二节)。

威胁模型: 公开演示里的 Agent 是一个任何人都能替我按的花钱按钮, 公网上不需要
恶意, 好奇的人循环点几十次就够。所以护栏是 llm_spend_daily 的
CHECK (spent_cny <= limit_cny), 不是应用层的一个 if:

- **预扣是事务内的一次加法** (INSERT ... ON CONFLICT DO UPDATE SET spent = spent
  + 预扣额), 并发由数据库的行锁串行化。不写成"先查余额, 够就扣" —— 那等于把
  护栏退回应用层, 十个人同时读到"还有余额", 十条任务全起来;
- CHECK 失败 = 今日额度用完, 异常向上抛, **让调用方的整个事务回滚** ——
  任务行连同预扣一起消失, 路由层翻成 429;
- 预扣额取单任务最坏情况 (agent_max_llm_calls × 保守单次估值, config 注释里有
  算式), 宁可预扣多了跑完回补, 不可预扣少了。

这是本项目第三次用"先预留再干活"这一手 (W3 start_drill、W4 reserve_task_slot),
但这次预留的东西在数据库里 —— 钱的上限必须跨进程、跨重启成立。

单账号配额 (user_task_quota_daily) 同一手、同一个事务: 它挡的不是钱, 是
"一个人把当天额度一次占光, 后面来的人什么都试不了"。

"今天"一律是 **UTC 日期** (SQL 里显式 AT TIME ZONE 'utc')。不用数据库的
current_date: 它跟会话时区走, 开发机在 Europe/London、服务器在 UTC 的话,
"今天"的边界会随部署地漂 —— 这条漂移不会有任何东西报错, 只会让某一天的额度
莫名多出或少掉一个小时。
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import settings

log = logging.getLogger(__name__)


class DailyBudgetExhausted(Exception):
    """全站日预算打满, 预扣被数据库 CHECK 拒绝 -> 429 (明天再来)。"""


class UserQuotaExhausted(Exception):
    """单账号当日任务数打满 -> 429 (明天再来, 或换一个演示账号)。"""


# 配额先于钱: 配额是按人的、信息更具体的那个拒绝理由; 两条 upsert 的加锁顺序
# 因此对所有事务一致 (先各自的配额行、再共享的花费行), 不会死锁。
_RESERVE_QUOTA = text("""
    INSERT INTO user_task_quota_daily (user_id, day, used, limit_count)
    VALUES (:user_id, (now() AT TIME ZONE 'utc')::date, 1, :limit_count)
    ON CONFLICT (user_id, day) DO UPDATE
        SET used = user_task_quota_daily.used + 1
""")

_RESERVE_SPEND = text("""
    INSERT INTO llm_spend_daily (day, spent_cny, limit_cny)
    VALUES ((now() AT TIME ZONE 'utc')::date, :hold, :limit_cny)
    ON CONFLICT (day) DO UPDATE
        SET spent_cny = llm_spend_daily.spent_cny + :hold
""")

# 回补是**幂等的结算**, 分两步、同一个事务:
#
# 1. 先抢 agent_tasks.hold_refunded_at 这把钥匙 (条件更新 WHERE ... IS NULL,
#    手法与 SPEC-003 状态机推进一致: 条件更新 + 数受影响行数)。抢不到 = 这笔
#    预扣已被结算过 (或任务行已被每日重置清掉), 一个字不写就返回。回补挂在
#    "任务进终态"上, 而进终态的路径有两条 (轮次收尾回调 / 清扫判死), 没有这把
#    钥匙同一笔钱会被减两遍 —— 台账越算越少, 护栏形同虚设;
# 2. 再回补: 差额 = 预扣额 - ai_usage 的真实合计, 两头都用 GREATEST 兜住 ——
#    实际花超预扣时不倒扣 (差额取 0), spent 也不许减成负数 (数据库另有
#    CHECK >= 0 守着同一件事)。数据来源是 ai_usage 的合计, 不是内存计数器:
#    与"调用数要数 ai_usage 的行" (SPEC-002 第九节) 同一条理由, 跨轮、跨进程、
#    跨重启都成立。回补落在**预扣那一天**的行上 (任务创建日的 UTC 日期):
#    跨天走完的任务把钱还回它扣走的那天, 而不是凭空减今天的账。
_CLAIM_SETTLEMENT = text("""
    UPDATE agent_tasks SET hold_refunded_at = now()
     WHERE id = :task_id AND hold_refunded_at IS NULL
    RETURNING id
""")

_REFUND = text("""
    WITH task AS (
        SELECT (created_at AT TIME ZONE 'utc')::date AS day
        FROM agent_tasks WHERE id = :task_id
    ), actual AS (
        SELECT coalesce(sum(estimated_cost_cny), 0) AS spent
        FROM ai_usage WHERE task_id = :task_id
    )
    UPDATE llm_spend_daily d
       SET spent_cny = GREATEST(0, d.spent_cny - GREATEST(0, :hold - actual.spent))
      FROM task, actual
     WHERE d.day = task.day
""")


async def reserve_task_budget(session: AsyncSession, *, user_id: int) -> None:
    """在调用方的事务里预扣: 账号配额 +1, 日花费 + 预扣额。

    任何一条被 CHECK 拒绝都抛异常 (事务随之整体回滚, 任务行一并消失)。
    只在新建任务时调用 —— 去重命中 (created=False) 的用户什么都没多得到,
    扣了就是白扣 (路由层守着这个条件)。
    limit 列只在当天第一次写入时定值: 行是当天的契约, 中途改配置不追溯。
    """
    cfg = settings()
    try:
        await session.execute(
            _RESERVE_QUOTA,
            {"user_id": user_id, "limit_count": cfg.agent_user_daily_tasks},
        )
    except IntegrityError as e:
        if "user_task_quota_within_limit" in str(e.orig):
            raise UserQuotaExhausted from None
        raise
    try:
        await session.execute(
            _RESERVE_SPEND,
            {"hold": cfg.agent_task_hold_cny, "limit_cny": cfg.llm_daily_budget_cny},
        )
    except IntegrityError as e:
        if "llm_spend_daily_within_limit" in str(e.orig):
            raise DailyBudgetExhausted from None
        raise


async def refund_task_hold(session: AsyncSession, task_id: int) -> None:
    """幂等结算: 抢到 hold_refunded_at 钥匙才回补, 抢不到什么都不动。

    钥匙与回补在同一个事务里 —— 回滚时钥匙一并回滚, 不会出现"标了已结算
    但钱没回去"的中间态。任务行不存在时 (每日重置清掉了在飞任务) 同样是
    0 行更新, 直接返回: 那笔预扣按 SPEC 已知边界随台账过夜清零。
    """
    claimed = await session.execute(_CLAIM_SETTLEMENT, {"task_id": task_id})
    if claimed.scalar_one_or_none() is None:
        return
    await session.execute(
        _REFUND, {"task_id": task_id, "hold": settings().agent_task_hold_cny}
    )


# ===== 回补的触发之一: 挂在后台任务轮的 done_callback 上 =====
#
# 另一处触发在清扫循环 (agent_runtime.sweep_once): 失联判死与 clarifying 超时
# 判死也是"进终态", 同样结算 —— 只挂这里的话, 停在 clarifying 被清扫判死的
# 任务预扣当天再也回不来, 而预扣 0.60 × 单账号配额 3 与 ¥3 日预算就差一笔,
# "点开、看到反问、关掉页面"恰恰是公开演示最常见的路径 (SPEC-009 第二节末尾)。
# 两处都走 refund_task_hold, 幂等钥匙保证撞上也只结算一次。
#
# 本回调只在本轮结局意味着"这条任务不会再发任何 LLM 调用"时结算:
# - awaiting_approval: 审批批不批都不再调模型;
# - awaiting_review (SPEC-008 报告任务): 人过目定稿或退回都不再调模型 ——
#   不加这一项, 每生成一份报告就永久占住一笔预扣不回补, 用户配额一份一份
#   漏光且不会报错 (第二段雷区 3);
# - failed / dead_letter: 终态;
# - clarifying **不结算**: 预扣按单任务最坏情况 (12 次调用跨轮累加) 估的,
#   人回答后恢复的轮次花的还是同一笔预扣; 一直没人答的由清扫判死时结算;
# - not_claimed / lease_lost: 任务归了别人 (或清扫), 这一轮不知道全貌, 不动账
#   —— 真正进终态的那条路径 (归属方的收尾, 或清扫) 会结算。
_REFUNDABLE_OUTCOMES = frozenset(
    {"awaiting_approval", "awaiting_review", "failed", "dead_letter"}
)

# create_task 返回的回补协程要留强引用, 否则可能跑完前被垃圾回收
# (与 agent_runtime._BACKGROUND_TASKS 同一条官方文档脚注)。
_REFUND_TASKS: set[asyncio.Task[None]] = set()


def refund_when_done(
    round_task: asyncio.Task[str],
    task_id: int,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """给 spawn 出去的任务轮挂回补钩子 (路由层在 spawn_task 之后调一次)。"""

    def _on_done(t: asyncio.Task[str]) -> None:
        if t.cancelled() or t.exception() is not None:
            # 异常退出的轮次由租约清扫收尸 (agent_runtime._discard_background
            # 已记日志), 账按 lease_lost 同一条边界处理: 不动
            return
        if t.result() not in _REFUNDABLE_OUTCOMES:
            return
        refund = asyncio.create_task(
            _refund_safely(task_id, factory), name=f"budget-refund-{task_id}"
        )
        _REFUND_TASKS.add(refund)
        refund.add_done_callback(_REFUND_TASKS.discard)

    round_task.add_done_callback(_on_done)


async def _refund_safely(
    task_id: int, factory: async_sessionmaker[AsyncSession]
) -> None:
    """回补是另一个事务, 且**回补失败不许让任务失败**: 钱多扣了是小事
    (差额留在台账里, 次日新行自然从零起算), 任务因为记账挂掉是大事。
    这里只记日志, 不向任何人抛。"""
    try:
        async with factory() as session, session.begin():
            await refund_task_hold(session, task_id)
    except Exception:
        log.exception(
            "task %s: 额度回补失败 (任务结果不受影响, 差额留在当日台账里)", task_id
        )
