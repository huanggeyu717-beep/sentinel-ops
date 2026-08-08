# Sentinel — AI-Native IoT Incident Automation Platform

> 将真实部署过的 AWS IoT 漏水监控系统重构为 AI 原生事故自动化平台:
> 确定性引擎负责检测与响应执行; AI 负责把自然语言运营规则编译为
> **经验证、可模拟、需审批**的响应策略, 并为已解决事故生成可审计报告。

## Quickstart

```bash
cp .env.example .env
docker compose up --build                    # db + api + web, 自动建表与写入演示基础数据
open http://localhost:8000/docs              # API 文档

# 灌入原系统真实历史读数 (344 条, 幂等, 可重复执行)
docker compose --profile replay up sim-replay
curl localhost:8000/status/sensors | jq

# 实时演示: 循环回放"同区多传感器无人响应升级"场景
docker compose --profile demo up device-sim
```

不想用 Docker 也可以裸跑: 起一个 Postgres, 设置 `SENTINEL_DATABASE_URL`, 然后
`uvicorn app.main:app --reload` (在 `apps/api` 下) + `python sim.py ...`。
建表由 API 启动时的 Alembic 迁移完成 (W2 起, 见 [ADR-006](docs/adr/ADR-006-alembic-migrations.md)),
裸跑 / CI / Docker 三条路径共用同一份迁移。

### 演示账号 (种子自动写入)

| 邮箱 | 密码 | 角色 |
|---|---|---|
| `admin@example.com` | `sentinel-demo` | admin (不绑现场员工) |
| `chris@example.com` | `sentinel-demo` | manager (绑员工 Chris Li) |
| `alex@example.com` | `sentinel-demo` | operator (绑员工 Alex Chen) |
| `dana@example.com` | `sentinel-demo` | manager (第二位 —— manager 自己写的策略必须由另一位批) |
| `viewer@example.com` | `sentinel-demo` | viewer (只读) |

`POST /auth/login` 登录后会话放在 httpOnly cookie 里, 浏览器里的 `/docs` 直接可用;
curl 场景可从登录响应的 `Set-Cookie` 取 token, 以 `Authorization: Bearer` 请求头携带。
员工 Bo Wang 刻意没有账号: 他只在现场刷卡, 从不登录 (见 SPEC-004)。

## 当前进度

| 周 | 内容 | 状态 |
|---|---|---|
| W1 | 骨架 / Compose / CI / 迁移 / **device-sim** / `/ingest` / `/status` | 完成, 见 [SPEC-000](docs/specs/SPEC-000-w1-ingest.md) |
| W2 | 事故生命周期 + RFID 接单 + JWT/RBAC + React Dashboard | 完成, 见 [SPEC-003](docs/specs/SPEC-003-incident-lifecycle.md) / [SPEC-004](docs/specs/SPEC-004-auth-rbac.md) / [SPEC-005](docs/specs/SPEC-005-dashboard.md) |
| W3 | Policy DSL + 双层验证器 + 引擎/模拟器 + 版本化审批发布 | 完成, 见 [SPEC-001](docs/specs/SPEC-001-policy-dsl.md) / [SPEC-006](docs/specs/SPEC-006-policy-lifecycle.md) |
| W4 | Agent 编排 + Automation Studio (最早可投递点) | 未开始 |
| W5 | 100 条评测集 + 消融实验 + Evals 面板 + 事故报告 | 未开始 |
| W6 | OTel + 可靠性 + MCP server + 免费托管上线 + 文档 | 未开始 |

## 为什么需要 Agent (而不是表单或聊天框)

跨传感器时间窗 + RFID 接单超时 + 通知/执行器联动的规则空间, 表单会组合爆炸;
但策略必须可靠, 所以 Agent 只负责理解与编译, 确定性引擎负责验证、模拟与执行,
发布必须人工审批。模型没有任何副作用执行权限。

## 结构

| 路径 | 内容 |
|---|---|
| `apps/api` | FastAPI 模块化单体 (auth/incidents/policies/agent) |
| `apps/web` | React + TS 操作台 (Dashboard / Automation Studio / Tasks / Evals) |
| `apps/device-sim` | 数字孪生模拟器 + 版本化场景包 (原硬件的软件替身) |
| `packages/policy_engine` | Policy DSL + 验证器 + 引擎 (纯函数, 执行=模拟) |
| `evals/` | 100 条评测集 + 确定性 grader + 消融实验 |
| `docs/` | specs / ADR / AWS 映射 / AI 研发证据链 |
| `legacy/` | 原 AWS 系统 (8 Lambda + IaC 备份), 只读证据 |

## 指标 (W5 后由 evals 产出, 不预设)

任务成功率 (A0→A2 消融) / 危险发布拦截率 / P50/P95 / tokens/task / cost/task

## 数据来源

`apps/device-sim/seed/waterlevel_readings.csv` 是原系统真实运行期间采集的 344 条水位读数,
由队友的分析仓库 [fengzhe-li/iot-sensor-data-analysis](https://github.com/fengzhe-li/iot-sensor-data-analysis)
导出。模拟器以它为基准回放, 因此演示与评测跑的是真实采样节奏与真实数值分布, 而非随机数。
其 2σ 统计阈值检测法将在 W5 作为基线臂参与对比。

## 前身

原系统 (Arduino MKR1010 ×2 + AWS IoT Core/Lambda/RDS/DynamoDB/SES) 曾真实部署运行,
端到端演示视频见 docs/。AWS 环境已按成本考虑注销, 组件映射见 docs/aws-mapping.md。
