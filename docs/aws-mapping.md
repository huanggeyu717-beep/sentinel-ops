# AWS 组件映射: 原系统 → Sentinel

| 原组件 | 原职责 | 重构后 | 变更理由 |
|---|---|---|---|
| IoT Core + MQTT + X.509 | 设备接入与路由 | device-sim → POST /ingest (保留 MQTT bridge 接口位) | 硬件不在手; 消息格式保持原样, 接入层可替换 |
| IoT Rules ×4 | SQL 路由到 Lambda | ingest 层事件规范化 + 引擎订阅 | 路由逻辑进代码, 可测试 |
| database-ingest Lambda | 解析入库 | services/ingest | 同逻辑 + 幂等键 + 测试 |
| automatic-alert Lambda | DRY→WET 边沿告警 + DynamoDB 去重 | 引擎内置系统策略 (吃自家 DSL 狗粮) | 告警规则策略化, 可被 Agent 管理 |
| status-api Lambda | 心跳超时/状态查询 | DeviceService (+60s 语义保留) | 补测试 |
| sensor-config Lambda | 阈值/开关下发 | ConfigService | 同 |
| led-control Lambda | 执行器控制 | ActuatorService (模拟器内虚拟 LED) | 执行器状态入 PG |
| manual-email-sender / eventbridge-email-notifier | 邮件 | NotificationService (dev: 控制台/MailHog) | SES 适配器保留接口位 |
| RDS PostgreSQL | 历史 | PostgreSQL (compose/Neon) | 保留 |
| DynamoDB ×2 | 低延迟状态 | PG 表 (量级不需要双存储) | ADR-003 |
| Cognito | 认证 (未接线, 前端硬编码密码) | 自建 JWT + RBAC | 真正接线并落 RBAC |
| SES | 邮件 | 适配器 | |
| S3+CloudFront | 前端分发 | Cloudflare Pages | 免费 |
| CloudWatch | 日志 | OTel + Jaeger + 结构化日志 | 链路视角 |
