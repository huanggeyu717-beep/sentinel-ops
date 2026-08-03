# ADR-001: 从 Serverless 重构为模块化单体

日期: 2026-08 | 状态: 已接受

背景: 原系统 8 个 Lambda 适合低运维常驻监控; 重构目标变为单人快速迭代、
任何面试官可一键复现、承载复杂 Agent 任务状态机。

决策: FastAPI 模块化单体 + Docker Compose。Lambda 归档 legacy/ 作为证据。

后果: (+) clone 到跑通 <10min; 状态机/事务/测试大幅简化。
(-) 放弃按事件源横向扩展 —— 通过 QueueBackend 与 ingest 适配层保留回迁缝,
量化阈值: 事件 >100/s 或需多 region 时重新评估。
