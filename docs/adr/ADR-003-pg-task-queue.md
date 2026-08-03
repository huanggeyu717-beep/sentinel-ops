# ADR-003: 任务队列用 Postgres 而非 SQS/Redis

日期: 2026-08 | 状态: 已接受

Agent 任务量级为单用户交互式 (<1 task/s), 需要的是: 状态可查询、超时重试、
幂等、失败留痕 (dead_letter 状态)。PG 表 + FOR UPDATE SKIP LOCKED + asyncio worker
以零额外基础设施满足全部需求, 且任务状态与业务数据同库同事务。
JD 明确反对"没有一致性需求却硬加消息队列"。
QueueBackend 为抽象接口; 阈值: >50 并发任务或需跨进程 worker 时切 Redis/SQS。
