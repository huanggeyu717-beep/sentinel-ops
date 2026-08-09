# W4 第一段 给 CC 的 prompt

本文件只写 SPEC 里没有的东西：文件边界、命名落位、易错点指路、报告格式。
**SPEC 里已经写过的内容一个字都不复述**——复述一遍就是同一个事实存两份，
改了 SPEC 这边不跟着改就走散。

---

## 任务

实现 SPEC-002 的**第一段**（见该文档末尾"分段实施"那一节）。

## 先读这些，按顺序

1. `CLAUDE.md`——架构不变量与工程约定
2. `docs/specs/SPEC-002-agent-orchestration.md`——**全篇，这是本段的唯一依据**
3. `docs/specs/SPEC-006-policy-lifecycle.md` 第二、三、五节——你要复用和修改的东西在这
4. `docs/specs/SPEC-001-policy-dsl.md` 第五节——错误码与 `Inventory` 的事实源
5. `docs/adr/ADR-006-alembic-migrations.md`——迁移一律手写、必须写 downgrade
6. `docs/adr/ADR-005-pin-lint-toolchain.md`——mypy 分档与白名单

现成可抄的形状：`alembic/versions/0007_policy_lifecycle.py`（迁移写法）、
`app/services/policy_runtime.py` 的 `tick_loop`（后台任务与干净取消）、
`app/services/incident_service.py`（条件更新 + 受影响行数为 0 即冲突）。

## 文件边界

**归你，可以自由新建和修改：**

- `apps/api/alembic/versions/0008_agent_orchestration.py`
- `apps/api/app/services/agent_service.py`（任务的增删查、去重、租约、状态推进）
- `apps/api/app/services/agent_runtime.py`（状态机主循环 + 后台打卡与清扫）
- `apps/api/app/services/agent_tools.py`（13 个工具的封装与注册表）
- `apps/api/app/services/inventory_service.py`（新建，只读：区/传感器/在册角色）
- `apps/api/app/services/llm_client.py`（模型客户端接口 + 本段的打桩实现）
- `apps/api/app/config.py`（只加本段需要的配置项）
- `apps/api/app/main.py`（**只允许**加后台循环的拉起与取消，照 `tick_task` 那段的形状；
  路由注册那一行先不动）
- `mypy.ini`（新模块加进严格档白名单）
- `apps/api/tests/test_agent_*.py`

**明确授权修改的 W3 文件**（只改 SPEC-002 第九节列的那几处，别顺手重构，
保住现有的审计写入与条件更新）：

- `apps/api/app/services/policy_service.py`

**冻结，一律不动**：`packages/**`、`apps/api/app/routers/**`、`apps/web/**`、
其余 `app/services/*.py`、`docs/**`（文档归评审方，你我并行时避免撞车）。
认为冻结文件里有真 bug 时**先停下来说**，不要擅自改。

**本段不做**：HTTP 路由、SSE、前端、真实 LLM。验收里凡是需要走 HTTP 才能验的，
本段跳过并在报告里点名；能在 service 层验的（并发去重、约束、租约、状态机）本段全做。

## 命名与落位

- 迁移 revision id：`0008_agent_orchestration`，`down_revision = "0007_policy_lifecycle"`
- 配置项（`config.py`，前缀 `SENTINEL_` 由 pydantic-settings 自动加）：
  `agent_heartbeat_seconds` / `agent_lease_timeout_seconds` / `agent_round_budget_seconds`
  / `agent_task_ttl_hours` / `agent_max_clarify_rounds` / `agent_max_llm_calls`
  / `agent_tool_timeout_seconds`。**打卡与判死两个值要就地注释写明它们的比例关系**
- 测试命名沿用 `test_<行为>__<条件>`

## 易错点指路

**迁移**

- `agent_tasks.idempotency_key` 上那个 UNIQUE 是**约束不是索引**，实名
  `agent_tasks_idempotency_key_key`。`DROP INDEX` 会失败。而且按 0007 立的规矩，
  **名字从 `pg_constraint` 查出来，不猜**——0007 里的 `_status_check_name()` 是现成写法，
  在 0008 里再写一份（迁移之间不共享代码是刻意的，各自锁住自己那一刻的库状态），
  但要注释说明为什么重复。
- `policy_versions.status` 的 CHECK 这次要改第二遍。0007 已经给它起了名字，
  但**仍然去 `pg_constraint` 查**，不要硬编码——这条库在不同人手上走过的路径不一样。
- `user_id` 设 NOT NULL 之前**先断言表内无 NULL 行**再加约束。现在这张表是空的，
  但迁移要能在任何一台机器上跑。
- downgrade 完整写，并**实测降一步再升回**。有损的地方（比如 `discarded` 状态回落）
  就地注释写清楚，照 0007 的做法。

**租约与并发**

- 那道闸必须是**一条 SQL**：条件写进 `WHERE`，受影响行数为 0 就停手。
  不要写成"先 SELECT 确认是我的，再 UPDATE"——中间那一瞬间情况可能已经变了。
- 打卡循环和清扫循环**搭在同一个后台任务上**，不要开两个 asyncio 任务。
- 关停时要能干净取消，否则测试会挂住（W3 的 tick 任务踩过，形状照抄）。
- `next_seq` 发号必须是原子的：`UPDATE ... SET next_seq = next_seq + 1 RETURNING next_seq`，
  且与写那条记录**在同一个事务**里。不要"先 SELECT 拿号再 UPDATE"。

**打桩模型**

- 打桩实现要能**按脚本吐一串固定响应**，不是只吐一个。修复循环的测试需要
  "第一次吐一个错的 zone、第二次吐对"这样的序列；澄清的测试需要"连吐两次错、
  第三次改口问人"。只能吐单条响应的打桩，这一段一半的测试写不出来。
- 打桩实现和将来的录制回放实现要是**同一个接口的两个实现**，接口在这一段就定死。

**工具层**

- 工具一律**调 service 函数，不自己拼 SQL**（不变量 4）。缺的只读能力就在
  `inventory_service.py` 里补，不要在工具层开后门。
- 单工具超时用 `asyncio.wait_for`，注意被取消时不要把半截状态写进库。
- 测试里**不许真连网**。

**lint**

- ruff 锁 `0.16.1`，配置在仓库根 `ruff.toml`，中文全角标点触发的 RUF001/002/003
  已整体 ignore，不用为标点改中文。
- 新模块必须加进 `mypy.ini` 的严格档白名单，否则新代码会悄悄退回默认档。
- 本机与 CI 跑的是同一份 `scripts/ci/lint.sh`，交活前跑一遍 `make lint`。

## 完成报告必须包含这几节

1. **做了什么**——按文件列，每个文件一句话。
2. **与 SPEC 不一致的地方，逐条列出。** 包括你认为 SPEC 写错了的地方——**给依据，
   可以直接提异议**。这一节宁可长，不要空。
3. **数据库结构的实测输出**——迁移跑完之后 `\d agent_tasks`、`\d agent_steps`、
   `\d agent_clarifications`、`\d policy_versions` 的真实粘贴，不是你描述的结果。
   外加 downgrade 一步再升回的结果。
4. **测试清单**——新增几条、每条锁的是哪个不变量。
5. **变异测试结果**——SPEC-002 验收 20、22、23 三条，逐条写：
   **我破坏了什么（具体到哪一行改成什么）→ 哪几条测试变红 → 红的是不是我预期的那几条。**
   如果破坏之后**照样绿**，那是本段最重要的发现，必须原样报上来，不要偷偷补测试再报绿。
6. **报数字必须带上产生它的配置**——任何耗时、条数、触发次数，都要附上跑出它的参数。
7. **越界改动清单**——改了边界之外的任何文件，逐条说改了什么、为什么。
8. **留给第二段的**——本段没做完或刻意推迟的，逐条列。

## 不要做的事

- 不接真实 LLM，不读 `SENTINEL_LLM_API_KEY`
- 不写 HTTP 路由、不写 SSE、不碰前端
- 不改 `docs/` 下任何文件
- **不执行任何 git 命令**（提交由本人在终端做）
