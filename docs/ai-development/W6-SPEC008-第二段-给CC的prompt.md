# SPEC-008 第二段 — 给 CC 的 prompt

> 用法(两种都行): 让 CC 直接读本文件, 或把分隔线之间的内容粘给它。
>
> **前置**: 第一段与它的修补都已完成并复核通过。**修补没做完不要开这一轮** ——
> 中文半边的口径会影响本轮给模型看的那段规则。
>
> **CC 读到这里请注意: 分隔线以下就是你这一轮要执行的任务本身, 不是背景资料。**

---

## 任务

实施 `docs/specs/SPEC-008-incident-report.md` 的**第二段**: 接进 W4 runtime
(三个工具 + 阶段序列) + 四个 HTTP 接口 + Studio 的报告页 → **打桩客户端跑通全流程**,
外加第六节那次并排对照进 README。

**先完整读 SPEC-008**, 重点第三、四、五、七、八节与第十节验收。
第一段复核之后 SPEC 又改过 (定稿决定已到 P), **不要照记忆里的旧版做**。

**本轮零真实模型调用。** 唯一花钱的是最后那次并排对照, 约 ¥0.2, 单列在最后一步,
前面全部用打桩客户端跑通。**没跑通全流程之前一分钱都不许花。**

## 文件边界

新增:

| 文件 | 内容 |
|---|---|
| `apps/api/app/services/report_task_service.py` | 建报告任务 / 落草稿 / 定稿 / 弃稿 / 取报告, 唯一写 `incident_reports` 的地方 |
| `apps/api/app/routers/reports.py` | 四个接口 (见 SPEC 第八节) |
| `apps/api/tests/test_reports_http.py` | 接口档 (照 `test_policies_http.py` 的写法) |
| `apps/api/tests/test_report_task_service.py` | service 档 |
| `apps/api/tests/test_report_runtime.py` | 阶段序列 + 打桩客户端跑通全流程 |
| `apps/web/src/features/studio/ReportPanel.tsx` | 报告页 |

修改 (**就这几处, 每一处都在下面"雷区"里点了名**):

- `app/services/agent_runtime.py` —— `run_task` 按 `task_type` 分派到新的 `_round_report`
- `app/services/agent_tools.py` —— 三个新工具 + `TOOLS_BY_STAGE` 两个新阶段
- `app/services/agent_prompts.py` —— 两个新阶段的提示词
- `app/services/agent_service.py` —— `finish_task` 与 `list_tasks` 认 `awaiting_review`
- `app/services/budget_service.py` —— `_REFUNDABLE_OUTCOMES` 加 `awaiting_review`
- `app/services/auth_service.py` —— 两个新权限点
- `app/main.py` —— 注册 reports 路由
- `mypy.ini` —— 两个新模块进白名单
- `README.md` —— 第六节那次并排对照
- `apps/web/src/api/*`, `features/studio/StudioPage.tsx` —— 接上报告页

**`report_render.py` / `report_service.py` / 迁移 0011 一律不动。** 第一段已定稿,
真发现必须动, **停下来在报告里说明为什么**, 不要顺手改了。

## 雷区 (这九处踩了就要返工, 都是评审方读代码找出来的)

1. **`agent_tasks.stage` 的数据库默认值是 `'parsing'`。** 建报告任务时**必须显式
   写 `stage='collecting'`** —— 不写就落 `parsing`, 而 `_round` 的 `parsing` 分支
   是策略那条路, 报告任务会一头栽进去跑策略流程。这是本轮最容易踩的一脚。
2. **`agent_service.finish_task` 里 `terminal = status != "awaiting_approval"` 是
   写死的。** `awaiting_review` 同样是"等人, 不是终态", 不加进去时间线上那一步会被
   标成终态。
3. **`budget_service._REFUNDABLE_OUTCOMES` 现在是
   `{awaiting_approval, failed, dead_letter}`。不加 `awaiting_review`, 每生成一份
   报告就永久占住一笔预扣不回补** —— 用户配额会一份一份地漏光, 而且不会报错。
   SPEC-009 的整套花钱护栏就靠这一格。
4. **`agent_service.list_tasks` 的 `ORDER BY` 里有
   `status IN ('running','clarifying','awaiting_approval')`** —— 不加
   `awaiting_review`, 等人过目的报告任务在列表里会排到最底下。
5. **`input_hash` 怎么产**: `create_task` 走 `normalize_input(input_text,
   target_policy_id)`, 而报告任务的输入是一个 incident_id 不是一句话。**定一个
   稳定的写法**(例如把 `f"incident_report:{incident_id}"` 当 input_text 喂进去),
   写进注释, 并配一条测试钉住 —— 它决定 `agent_tasks_one_open` 那个部分唯一索引
   还认不认得出重复。
6. **去重不能只靠那个索引。** `agent_tasks_one_open` 是
   `(user_id, input_hash) WHERE status IN ('running','clarifying')` —— **只挡同一个
   用户**, 且不含 `awaiting_review`。两个不同的用户同时点同一条事故会开出两个任务,
   第二个跑到 `drafting` 撞上 `incident_reports_one_active` 直接 failed, 用户看到的
   是"任务失败"而不是"已经有人在生成了"。**service 要自己查一次"该事故有没有未走完
   的 incident_report 任务"** (SPEC 第八节), 命中返回 200 + 既有 task_id。
7. **复用 `run_task` 的外壳, 只分派循环体。** `claim_task` / 租约 / 单轮预算 /
   `_advance` / `_tool_step` / `_llm_call` / `_fail` / 那一整套失败出口分类
   (`model_protocol_error` / `llm_timeout` / `llm_error` / `replay_miss` /
   `round_budget_exceeded` / `lease_lost`) **全部照用, 一个都不要重写** ——
   六周的教训都长在那里面。新写的只有 `_round_report` 这个循环体, 因为阶段确实不同。
   `run_task` 里那句 `stage=task["stage"] if task["stage"] != "clarifying" else
   "discovering"` 是策略专用的, **别让它改写报告任务的 stage**。
8. **报告任务不进消融臂。** `_stage_tools` 会按 `AblationProfile` 裁剪工具清单,
   而那些档位是给策略编译定义的。报告的三个工具**不受能力档影响**, 写清楚并配测试;
   同时确认 `scripts/ci/test-eval-smoke.sh` 不会把报告任务卷进去。
9. **`incident_reports.updated_at` 只有 `DEFAULT now()`, 没有触发器。**
   修复循环里就地改草稿时 service 必须自己更新它, 否则它永远是创建时刻。

## 另外几处 SPEC 已经定死、别自己另发明的

- **权限点是代码常量不是表** (`auth_service._ROLE_PERMISSIONS`):
  `reports:draft` 与 `reports:finalize` **都给 operator+**, **不做"事故处理人"这种
  行级归属判断** (SPEC 第八节写了理由)。**不需要迁移。**
- **两个终态分开** (SPEC 第四节): 人定稿 → 任务 `completed` + 报告 `final`;
  人退回 → 任务 `completed` + 报告 `discarded`; 修满 2 次不过 → 任务 `failed` +
  报告 `discarded`。**`failed` 专指"模型没写对"**, 人退回不许落 `failed`。
- **`discard` 允许作用在 `final` 上**, 弃稿一律进 `audit_log`。
- **没有 `ask_clarification`。** 事故已经结了, 现场没人可问。
- **并排对照里"模板铺开的那一份"不入库** —— 入库会被
  `incident_reports_one_active` 拦住。它是脚本产出, 直接进 README。

## 给模型看的那段规则 (系统提示词, 本轮的核心之一)

`drafting` 与 `repairing` 两个阶段的提示词里要把占位符规则写死, **并给正反例**:
正文里**只能**写 `{{fact_id}}`, 不许自己写任何数字与专名; 事实缺失时照常引用那个 id
(渲染器会吐出"无此记录"); 校验打回时 user 消息里要带上**错误码 + 违规的那个片段**,
不能只说"有错"。

`repairing` 的提示词必须带上模型上一版的草稿 (照 `_RoundState.last_body` 那套做法)
—— **不给草稿等于让模型盲改**, 这是 W4 已经踩过的坑。

## 变异测试

SPEC 第十一节第 3 条归本段: **`fact_pack` 改成渲染时重算而不是读快照 → 应有测试红**
(造一条事故, 生成报告后往时间线补一条事件, 断言正文不变)。

本段自己新增的关键不变量, 每条都要**真改坏、真跑红、再改回**:

1. 把 `_REFUNDABLE_OUTCOMES` 里的 `awaiting_review` 拿掉 → 应有测试红
   (断言报告任务停在等人过目时预扣**已回补**);
2. 建任务时不显式写 `stage='collecting'` → 应有测试红;
3. 去掉 service 那一次"有没有未走完的报告任务"查询 → 应有测试红
   (**用两个不同的用户**造这条用例, 只用同一个用户的话既有索引会替它挡住, 测不出来);
4. 把"人退回"改成落 `failed` → 应有测试红。

## 交之前必须跑的(五个脚本, 一个都不能少)

```
bash scripts/ci/lint.sh
bash scripts/ci/test-unit.sh
bash scripts/ci/test-api.sh
bash scripts/ci/test-eval-smoke.sh
bash scripts/ci/test-docker.sh
```

前端另跑 `apps/web` 的构建与类型检查 (照仓库现有做法, 别自己发明命令)。
`make lint` 之前先 `make lint-version`。

## 最后一步单列: 并排对照 (SPEC 第六节, 约 ¥0.2)

**全流程用打桩客户端跑通、五个脚本全绿之后**才做这一步, 并且**先停下来在报告里
说明你打算跑几次、预计花多少**, 等人确认再跑。挑一条事故, 把"模板把事实全铺开"
的那份与 Agent 写的那份并排放进 README。

README 里**只说这是一个例子, 不给任何百分比** —— 没有覆盖率与冗余率那两个数,
"模型的价值在取舍不在产生事实"这句话是靠一个例子撑着, 不是靠一个统计量
(SPEC 第六节原话)。**不出现"幻觉率"这个词。**

## 完成报告

写进 `docs/ai-development/W6-SPEC008-第二段-完成报告.md`, 固定三节:

1. **与 SPEC 不一致的地方** (含你认为 SPEC 写错的)。照字面实现不通就停下来上报,
   不要自己改口径悄悄绕过去;
2. **自行新增的分支 / 写操作各由哪条测试守着** —— 点名测试函数名, 一个都不许漏;
3. **报数字必须带产生它的配置**。

外加: **五个 CI 脚本各自的退出码** + **上面九处雷区逐条说明你怎么处理的**。

## 红线

- git 写操作一律不碰 (`add` / `commit` / `push` / `checkout` 都不行);
- commit message 不写内部角色与返工过程;
- 密钥只进 `.env`; 临时脚本放 `scripts/dev/`;
- 文档与 README 不用表情符号与装饰性图标。

---
