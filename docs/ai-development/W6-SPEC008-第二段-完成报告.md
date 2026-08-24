# W6 SPEC-008 第二段 — 完成报告

> 执行: CC, 2026-08-23。范围: 报告任务接进 W4 runtime (三个工具 + 阶段序列) +
> 四类 HTTP 接口 (五个端点) + Studio 报告页, 打桩客户端跑通全流程。
> **本轮零真实模型调用、零花费**: 全部用 ScriptedLLMClient, 没有录制、没有真跑。
> **最后一步"并排对照"按本人指令整个不做** (覆盖任务书), 见文末单独一节。

## 一、与 SPEC 不一致的地方

1. **无实质不一致**: 阶段序列 (collecting → drafting → validating → repairing,
   通过即 awaiting_review)、三工具、无 ask_clarification、两个终态分开
   (人定稿/人退回 → completed, 修不过 → failed)、去重判据"未走完的报告任务"、
   202/200/422 口径、权限 operator+ 不做行级判断、discard 允许作用在 final 上并
   进 audit_log —— 全部按 SPEC 第四、五、七、八节字面落地。

2. **四处 SPEC 未定口径, 实现自定并有测试钉住**:
   - **报告 prompt 的版本号独立编 `r1`** (REPORT_PROMPT_VERSION), 不动策略的
     v3。agent_prompts 的规矩是"改进模型输入的内容必须换号", 但报告是**新增**
     一套 system prompt 而非改动既有的 —— 与 v3 混用一个号, ai_usage 的
     prompt_version 列分不清任务类型, 回放键也串味; 沿用 PROMPT_VERSION_A0
     分号的先例;
   - **定稿只在任务 awaiting_review 时允许** (409 otherwise): 任务还在跑时
     定稿等于把一个模型还会改写的东西钉成终稿
     (`test_finalize_report__conflicts`);
   - **任务还在跑时不许退回草稿** (409): 弃了草稿, 模型下一步就地改会扑空,
     变成一次假的系统故障 (`test_discard_final__allowed_and_running_draft_blocked`);
   - **`TOOLS_BY_STAGE` 的报告键带 `report_` 前缀** (`report_drafting` /
     `report_repairing`): 报告的 repairing 与策略的 repairing 同名不同工具,
     不能共键 (`collecting` 无冲突, 不加前缀)。

3. **文件边界外的改动一处, 结构性必需**: `apps/api/tests/test_agent_tools.py`
   的 `SPEC_TOOLS` 分级表补了三个报告工具。该测试的设计就是"注册表与分级表
   完全相等, 加工具必须让它红一次、逼人补分级" —— 本轮它如约红了, 按它的
   设计意图更新, 不是绕过。

4. **既有接口 `GET /agent-tasks` 的 status 过滤枚举 (TaskStatusFilter) 没加
   `awaiting_review`**: `agent_tasks.py` 不在本轮修改清单里。影响只有一条 ——
   HTTP 上不能按 awaiting_review 过滤任务列表; 列表本身照常返回这类任务且
   排在最前 (雷区 4 已修)。留给下一轮, 在此报备。

## 二、自行新增的分支 / 写操作, 各由哪条测试守着

**写操作** (全部收在 `report_task_service.py`, 唯一写 `incident_reports` 的地方):

| 写操作 | 守着它的测试 |
|---|---|
| 建任务 + 显式 `stage='collecting'` (雷区 1) | `test_create_report_task__stage_explicitly_collecting`; 变异 B2 时 runtime 档 6 条连带红 |
| 落草稿 (body + fact_pack 快照 + created_by) | `test_report_happy_path__facts_draft_validate_to_awaiting_review` |
| 就地改草稿 + **手工盖 updated_at** (雷区 9) + previous_body 进时间线 | `test_update_report_draft__bumps_updated_at_and_returns_previous`、`test_report_repair_loop__violation_fed_back_then_fixed` |
| 校验计数按违规项累加进两列 | `test_validate_task_report__counts_accumulate_per_item` |
| 定稿: 报告 final + 任务 completed + 时间线 decision=finalized | `test_finalize_report__draft_to_final_task_completed`、HTTP 档 `test_finalize__operator_can_then_repeat_conflicts` |
| 弃稿: draft/final 均可 + audit_log + 人退回落 completed | `test_discard_draft__task_completed_not_failed` (变异 B4)、`test_discard_final__allowed_and_running_draft_blocked`、HTTP 档 `test_discard_final__frees_slot_for_regeneration` |
| 失败路径弃稿 (`discard_task_report`, `_fail` 与清扫两处都调) | `test_report_repairs_exhausted__failed_and_report_discarded`、`test_report_sweep__stale_task_dead_letter_discards_draft` |

**runtime 侧自行新增的分支** (都在允许修改的 `agent_runtime.py` 内, 但超出
"加一个 `_round_report`"的字面, 逐条列):

| 分支 | 理由 | 守着它的测试 |
|---|---|---|
| `_fail` 里补一句 `discard_task_report` | SPEC 第四节: 修不过/死信 → 报告 discarded; 策略任务 0 行更新无感 | `test_report_repairs_exhausted__failed_and_report_discarded`、`test_report_wrong_tool__model_protocol_error` (策略档 45 条回归全绿证明无感) |
| `sweep_once` 里同一句 | 判死不经轮次收尾, 只挂 `_fail` 会漏清扫这条路 | `test_report_sweep__stale_task_dead_letter_discards_draft` |
| `_llm_call` 拆出共用的 `_llm_complete` | 报告循环体复用同一套调用上限与 ai_usage 落账, 不复制第二份 (雷区 7 的精神); 策略路径行为不变 | 既有 `test_agent_runtime` 30 条全绿 + `test_agent_ablation` 回归 |
| `_summarize` 认两个报告草稿工具 (保留 previous_body) | 修复前的中间态只存在时间线里, 与策略同规 | `test_report_repair_loop__violation_fed_back_then_fixed` (间接) |
| `run_task` 对报告任务跳过 `clarifying→discovering` 改写 | 雷区 7 点名: 那句是策略专用 | 报告档全部用例都从 collecting 起跑 |

**HTTP 层**: 路由零业务逻辑, 异常翻译与 429 三分类照抄 agent_tasks 的形状;
权限档位由 `test_report_permissions__viewer_reads_but_cannot_write` 钉住
(viewer 读 200、写 403、未登录 401)。

## 三、数字与产生它的配置

- **新增测试 29 条, 全绿**: `test_report_task_service.py` 14 条 +
  `test_report_runtime.py` 7 条 + `test_reports_http.py` 8 条;
  从仓库根跑, 库为 docker-compose Postgres 16 (宿主机 5433, sentinel_test)。
- **api 档全量 410 passed / 1 skipped** (`bash scripts/ci/test-api.sh`),
  第二段前是 381 —— 净增即上面 29 条, 既有 381 条零回归。
- **单元档 299 passed**, 与第二段前持平 (本段没动纯函数档)。
- **前端**: `cd apps/web && npm run build` (tsc --noEmit 严格档 + vite build)
  通过, 112 模块, 产物 251.89 kB (gzip 81.23 kB)。
- **零花费的证据链**: 所有用例走 `ScriptedLLMClient` (estimated_cost_cny=0);
  `.env` 的 key 未被任何测试读取; 冒烟评测 (test-eval-smoke) 是回放模式,
  10 条全过零 miss —— 数据集只有 policy_compile 用例, 报告任务不在其中
  (雷区 8 后半的确认)。

## 四、五个 CI 脚本退出码

| 脚本 | 退出码 | 备注 |
|---|---|---|
| `bash scripts/ci/lint.sh` | 0 | ruff 0.16.1 + mypy 严格档 (report_task_service 与 routers.reports 已入白名单), 153 个文件 |
| `bash scripts/ci/test-unit.sh` | 0 | 299 passed |
| `bash scripts/ci/test-api.sh` | 0 | 410 passed, 1 skipped |
| `bash scripts/ci/test-eval-smoke.sh` | 0 | 10 条全过, 零回放 miss, 零注入得逞 |
| `bash scripts/ci/test-docker.sh` | 0 | 容器内迁移与断言全过 |

外加前端构建与类型检查 (见第三节), 退出码 0。

## 五、九处雷区逐条说明

1. **stage 默认值 'parsing'**: `create_report_task` 建行后同一事务显式
   `UPDATE stage='collecting'`; 变异 B2 (去掉这句) 红 7 条测试。
2. **finish_task 的 terminal 写死**: 改成
   `status not in ("awaiting_approval", "awaiting_review")`;
   守着它的断言是 happy path 里 `completed_at is None`。
3. **_REFUNDABLE_OUTCOMES**: 加了 `awaiting_review`; 变异 B1 (拿掉) 红
   `test_hold_refunded__when_round_ends_awaiting_review` —— 该用例真走
   `refund_when_done` 回调 + 真库台账, 断言预扣回补到分毫。
4. **list_tasks 的 ORDER BY**: `awaiting_review` 已加进"未走完"分组,
   等人过目的报告任务排在列表最前。
5. **input_hash 怎么产**: 统一喂 `incident_report:{incident_id}`
   (`report_input_text()`), 注释与 `test_report_input_text__pinned_format`
   双重钉住。
6. **去重不能只靠索引**: service 按 input_hash 自查"未走完的报告任务"
   (running / awaiting_review, **跨用户**); 变异 B3 用**两个不同用户**造,
   红 2 条 —— 只用同一个用户的话既有索引会替它挡住, 测不出来。
7. **复用 run_task 外壳**: 只新写了 `_round_report` 循环体与
   `_report_llm_call` 的消息拼装; claim/租约/单轮预算/`_advance`/`_tool_step`/
   `_fail`/六类失败出口全部原样走 (拆 `_llm_complete` 是提取不是重写,
   策略档 45 条既有测试零改动全绿)。`clarifying→discovering` 那句改写对
   报告任务被显式绕开。
8. **报告不进消融臂**: 三个工具不经 `_stage_tools` 裁剪, 直接取
   `TOOLS_BY_STAGE` 的 report_ 键;
   `test_report_tools_immune_to_ablation__a0_profile_same_flow` 用 A0 档
   跑完整流程断言工具清单一字不差。冒烟评测只含 policy_compile 用例,
   报告任务卷不进去 (第三节)。
9. **updated_at 无触发器**: `update_report_draft` / 计数累加 / 定稿 / 弃稿
   每一处 UPDATE 都自己盖 `updated_at = now()`;
   `test_update_report_draft__bumps_updated_at_and_returns_previous` 钉住。

## 六、变异测试 (真改坏、真跑红、再改回)

| # | 改坏的是哪一行 | 红的是哪条测试 |
|---|---|---|
| B1 | `_REFUNDABLE_OUTCOMES` 拿掉 `awaiting_review` | `test_hold_refunded__when_round_ends_awaiting_review` |
| B2 | `create_report_task` 里删掉 `UPDATE stage='collecting'` | `test_create_report_task__stage_explicitly_collecting` + runtime 档 6 条 (报告任务栽进策略 parsing 分支) |
| B3 | 跨用户去重查询改成恒 `existing = None` | `test_create_report_task__dedupe_across_users`、`..._covers_awaiting_review` |
| B4 | 人退回改成把任务落 `failed` | `test_discard_draft__task_completed_not_failed` |
| SPEC 11.3 | `get_report` 的 fact_pack 改成渲染时重算 (`load_fact_pack` 现查) 而不是读快照 | `test_report_snapshot__rendered_body_unchanged_after_new_timeline_event` (生成后往时间线前面补一条事件, tl_1 重算会变) |

改回后 29 条 + 全量 410 条复归全绿, 五个 CI 脚本在还原后跑, 全 0。

## 七、并排对照: 留着没做 (本人指令, 覆盖任务书)

任务书原定最后一步"挑一条事故, 模板铺开的一份 vs Agent 写的一份并排进
README" (约 ¥0.2, 需一次真实模型调用)。**本人明确指示这一步整个不做,
一次真实调用都不发**。因此:

- README 未动, 没有并排对照一节;
- 本轮通过打桩验证的是**机制** (占位符渲染、两道硬拦、修复回路、终态口径),
  没有任何关于"Agent 写得比模板好"的展示或断言 —— 那句话本来就只能靠
  真跑的例子撑, 没跑就不说;
- 若日后要补: 全流程与五个端点已就绪, 只差挑一条事故各生成一份、把两段
  文字贴进 README, 花费约 ¥0.2。

## 八、改动文件清单与建议 commit message

新增:

- `apps/api/app/services/report_task_service.py`
- `apps/api/app/routers/reports.py`
- `apps/api/tests/test_report_task_service.py` / `test_report_runtime.py` /
  `test_reports_http.py`
- `apps/web/src/features/studio/ReportPanel.tsx`
- `docs/ai-development/W6-SPEC008-第二段-完成报告.md` (本文件)

修改:

- `apps/api/app/services/agent_runtime.py` (task_type 分派 + `_round_report` +
  失败路径弃报告 + `_llm_complete` 提取)
- `apps/api/app/services/agent_tools.py` (三个报告工具 + TOOLS_BY_STAGE 三键)
- `apps/api/app/services/agent_prompts.py` (REPORT_PROMPT_VERSION=r1 +
  报告两阶段 prompt 带正反例 + 三个工具 Schema)
- `apps/api/app/services/agent_service.py` (finish_task 认 awaiting_review;
  list_tasks 排序)
- `apps/api/app/services/budget_service.py` (`_REFUNDABLE_OUTCOMES`)
- `apps/api/app/services/auth_service.py` (`reports:draft` / `reports:finalize`)
- `apps/api/app/main.py` (注册 reports 路由)
- `mypy.ini` (两个新模块入严格档白名单)
- `apps/api/tests/test_agent_tools.py` (分级表补三工具, 见第一节第 3 条)
- `apps/web/src/api/types.ts` / `queries.ts`、
  `apps/web/src/features/studio/StudioPage.tsx` (接上报告页 + awaiting_review 文案)

建议 commit message (git 由本人执行):

```
feat(report): 事故报告第二段 —— 接入 runtime 三工具四接口与 Studio 报告页 (SPEC-008)

- 报告任务走 W4 runtime 外壳: 只新增 _round_report 循环体, 租约/预算/
  失败出口全复用; collecting 显式入栈, 不受消融能力档影响
- 去重跨用户 (service 自查"未走完的报告任务"), 命中返回 200 + 既有 task_id;
  awaiting_review 计入回补与列表排序, 预扣不再漏
- 五个端点: 生成 202 / 两个 GET (渲染只读快照) / 定稿 / 弃稿 (final 可弃,
  一律进 audit_log); 人退回落 completed, failed 专指模型没写对
- 打桩客户端跑通全流程, 零真实调用; 新增测试 29 条, 变异 5 项逐一改坏验红;
  五个 CI 脚本与前端构建全绿。并排对照留待后补
```
