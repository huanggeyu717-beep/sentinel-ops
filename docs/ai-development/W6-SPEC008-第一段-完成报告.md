# W6 SPEC-008 第一段 — 完成报告

> 执行: CC, 2026-08-23。范围: 迁移 0011 + `build_fact_pack` 纯函数 + 占位符渲染器 +
> 两道硬拦 + 单元与变异测试。第二段 (runtime / HTTP / Studio) 一行未写。

## 一、与 SPEC 不一致的地方

1. **`E_BARE_FACT` 判据 2 的字面单位表与验收互斥, 已改用两字单位并在代码注释报备。**
   SPEC 第三节判据 2 的单位表写的是 `分 / 秒 / 时 / 天 / 次 / 条 / 人 / 区 / 级`,
   但按字面实现, 验收里点名要**放行**的两个用例当场被拦: "第**一时**间"命中
   `一+时`, "**十分**明显"命中 `十+分` (后者还是判据 2 自己举的正常中文例子)。
   判据与验收在字面上二选一, 实现取了验收: 时长单位改用两字的 `分钟 / 小时 / 秒钟`
   (`秒/天/次/条/人/区/级` 维持单字)。代价: "三分"、"一时"这类不带"钟"的中文时长
   裸写漏过 —— 但同一事实的阿拉伯写法 ("3 分") 仍被判据 2 前半兜住。
   位置: `report_render.py` 的 `_CN_QUANTITY_RE` 注释。

2. **`tl_truncated` 只在真发生截断时产出** (SPEC 第二节字面如此)。这与第二节
   ""id 集合变成静态清单""那句存在张力: 时间线条数本来就是动态的, `E_DANGLING_REF`
   实现为查**本次事实包**的 id 集合 —— 固定 17 条恒在 (产全机制保证), tl 部分动态。
   引用被截掉的 `tl_21` 会被拦, 有测试钉住
   (`test_dangling_ref_truncated_timeline__dropped_tl_id_is_dangling`)。

3. **`load_incident_facts` 的连接参数按仓库约定实现为 `session: AsyncSession`**,
   任务书里写的是 `load_incident_facts(conn, incident_id)`。services/ 层全部模块
   (incident_service / budget_service 等) 走 SQLAlchemy AsyncSession, 裸 asyncpg
   连接只在测试夹具里用; 函数名、职责、"只有它读库"的边界与任务书一致。

4. **`test_report_render.py` 自身零库调用, 但收集它时库仍会被连上**:
   `apps/api/tests/conftest.py` 的 autouse 夹具 (`clean_telemetry` -> `client`)
   对目录里每条测试生效。这是目录级机制, 不属于本文件能控的范围; 被测模块的
   纯度由传递 import 断言守着, 与"测试进程连没连库"是两件事。

5. **两处 SPEC 未定口径, 实现自定并写进注释**:
   - `sensor_30d_count` 的 30 天窗口**锚在本事故的 opened_at** (不锚查询时刻,
     否则同一条事故明天生成这个数会变), 计数**含本事故自己** (恒 >= 1);
   - `resolved_by` 出现三前缀之外的值 (如 W2 旧口径 `auto_sensor_dry`) 时,
     `resolved_kind` 按缺失处置 (`无此记录`), 不猜。

6. **`incident_reports.status` 的 CHECK 起了名字** (`incident_reports_status_check`),
   SPEC 第七节 DDL 里是匿名内联 —— 0011 自己就在给 0001 的匿名约束收尸,
   不再造一个同款; 迁移测试按名字点到它。

## 二、自行新增的分支 / 写操作, 各由哪条测试守着

**写操作**: 本段唯一的写库代码是迁移 0011 本身 (建表、索引、两条 CHECK、
downgrade)。service 层只有读 (`load_incident_facts`), 纯函数层零 IO。

**文件边界外的改动 (三处, 全部同一个结构性原因)**: `incident_reports` 带
`incident_id -> incidents`、`task_id -> agent_tasks` 两条外键, 而 Postgres 的
TRUNCATE 要求引用表同列 —— 不改, 既有的清表语句在 0011 之后**全体报错**
(不是新用例挂, 是每个用例的夹具挂):

| 文件 | 改动 | 守着它的测试 |
|---|---|---|
| `apps/api/tests/conftest.py` | `TELEMETRY_TABLES` 加 `incident_reports` | 整个 api 档 (365 条) 的夹具本身 |
| `apps/api/tests/test_agent_helpers.py` | `AGENT_TABLES` 加 `incident_reports` | `test_agent_*` 与 `test_migration_0011` 的夹具本身 |
| `scripts/ops/reset_demo_data.py` | `DEMO_TABLES` 加 `incident_reports` (报告也是访客数据, 语义上就该清) | `test_reset__with_marker_clears_junk_keeps_seed_and_ledger` (改前实测红, 改后绿) |

**实现里自行新增的分支**:

| 分支 | 守着它的测试 |
|---|---|
| 三个 SPEC 未命名的错误码: `E_RAW_OVERFLOW` (渲染前 800 硬顶) / `E_RENDERED_OVERFLOW` (渲染后字段上限) / `E_BAD_SHAPE` (字段缺失/多余/非字符串) | `test_char_limit_before_render__800_hard_cap_on_raw` / `test_char_limit_after_render__rendered_overflow_rejected` / `test_check_draft_shape__missing_field_rejected` 与 `__extra_field_rejected` |
| 超长与形状错**不计入**两个倾向计数 (它们是格式问题不是编造倾向) | 上述两条 overflow 测试各自断言 `bare_fact_attempts == 0` |
| 专名先查先剔再扫数字 (一处裸写的 "3 号传感器" 算 1 项, 不按内嵌数字重复计) | `test_bare_fact_counts_proper_noun__employee_name_written_out` (断言恰为 1) |
| 缺失专名 (value 为 None) 不进裸写黑名单 —— 正文写"无此记录"四个字不算裸写 | `test_bare_fact_skips_missing_facts__wu_ci_ji_lu_not_blacklisted` |
| 截断后 tl 序号按保留顺序重排为 `tl_1..tl_20` (id 是引用句柄, 原始事件在 value 里) | `test_fact_pack_timeline_truncation__over_twenty_keeps_head5_tail15` |
| `cross_zone` 判据: 员工**当前** zone 与事故 zone 比对, zone 为空按跨区 (SPEC-003 决策 7 同口径); 未派单时整条缺失 | `test_fact_pack_cross_zone__mismatch_and_null_zone_both_count`、`test_fact_pack_keeps_missing_entry__no_ack` |
| downgrade 时 `awaiting_review` 行回落 `failed` (降级后报告表已不存在, 任务走不下去也定不了稿; 有损, 迁移注释注明) | `test_migration_0011__downgrade_then_upgrade_roundtrip` (真降真升) |
| `render_body` 撞未知 id 抛 ValueError, 不静默保留原文 | `test_render_body_raises__dangling_ref_not_silently_kept` |

## 三、数字与产生它的配置

- **新增测试 48 条, 全绿**: 从仓库根 `pytest apps/api/tests/test_report_render.py`
  36 条 + `pytest apps/api/tests/test_migration_0011.py` 7 条 +
  `pytest apps/api/tests/test_report_service.py` 5 条; 数据库为
  docker-compose 的 Postgres 16 (宿主机 5433, 库 sentinel_test)。
- **api 档全量 365 passed / 1 skipped**: `bash scripts/ci/test-api.sh`
  (= `pytest apps/api/tests evals/runner/tests -q`, 同一个库)。
- **单元档 299 passed**: `bash scripts/ci/test-unit.sh`
  (= `pytest packages apps/device-sim evals/tests -q`, 不连库)。
- **mypy 严格档零报错**: 两个新模块已加入 `mypy.ini` 白名单同一行列表,
  由 `bash scripts/ci/lint.sh` (ruff 0.16.1 + mypy, 149 个文件) 覆盖。
- **迁移往返在两个库上各真跑一次**: sentinel_test 由
  `test_migration_0011__downgrade_then_upgrade_roundtrip` 跑
  (显式 `downgrade 0010_deploy_guardrails` 再 `upgrade head`); 开发库 sentinel 由
  `make migrate` -> `make migrate-down` -> `make migrate` 跑, 终态
  `alembic current` = `0011_incident_reports (head)`。

## 四、五个 CI 脚本的退出码

| 脚本 | 退出码 | 备注 |
|---|---|---|
| `bash scripts/ci/lint.sh` | 0 | 之前先过 `make lint-version` (ruff 0.16.1 与 requirements-dev.txt 一致) |
| `bash scripts/ci/test-unit.sh` | 0 | 299 passed |
| `bash scripts/ci/test-api.sh` | 0 | 365 passed, 1 skipped。**第一遍跑是红的**: `test_reset_script` 撞上 TRUNCATE 外键 (上文第二节第三处改动), 补上 `DEMO_TABLES` 后重跑整个脚本全绿 |
| `bash scripts/ci/test-eval-smoke.sh` | 0 | 冒烟 10 条全过, 零回放 miss, 零注入得逞 |
| `bash scripts/ci/test-docker.sh` | 0 | 容器内跑迁移与断言, 0011 在 compose 环境同样过 |

## 五、变异测试 (SPEC 第十一节, 第一段做 1 / 2 / 4 / 5 / 6 / 7)

每条都是**真改坏、真跑、看红、改回**, 改回后 48 条复归全绿。

| # | 改坏的是哪一行 | 红的是哪条测试 |
|---|---|---|
| 1 | `check_bare_facts` 函数体首行插 `return []` | `test_bare_fact_counts_arabic_digits__one_per_run`、`..._chinese_quantity__number_plus_unit_only`、`..._proper_noun__employee_name_written_out`、`..._three__three_violations_in_one_round`、`..._zone_name__proper_noun_from_pack` (5 条红) |
| 2 | `check_dangling_refs` 函数体首行插 `return []` | `test_dangling_ref_rejected__unknown_id`、`..._counts_each__two_unknown_refs`、`..._truncated_timeline__dropped_tl_id_is_dangling` (3 条红) |
| 4 | 真库里 `DROP INDEX incident_reports_one_active` (等价于迁移里拆掉那条 CREATE) | `test_incident_reports_one_active__second_nondiscarded_blocked` 红; 重建索引前还得先 TRUNCATE —— 红跑期间第二行 draft 真的插进去了, 这正是索引在挡的东西 |
| 5 | `DraftCheckResult.bare_fact_attempts` 改成 `return 1` | **两个方向一次看全**: "正常输入 -> 计数为 0"方向红了 `test_bare_fact_zero__clean_placeholder_prose` 与 4 条 `test_bare_fact_allows__idiomatic_chinese`; "该加的时候加对了数"方向红了 `..._counts_arabic_digits` (期望 2)、`..._counts_three` (期望 3) 等, 共 12 条红 |
| 6 | `_strip_placeholders` 改成返回原文 (不剔除 `{{...}}`) | `test_bare_fact_ignores_placeholder_digits__timeline_refs` 红 ({{tl_3}}/{{tl_12}} 被自己的数字拦下) |
| 7 | `ack_by` 缺失分支改成不产条目, 并同时注释掉产全自检断言 | `test_fact_pack_keeps_missing_entry__no_ack`、`test_render_missing_fact__yields_wu_ci_ji_lu_not_a_name`、`test_report_service.py::test_facts_end_to_end__unacked_incident_renders_missing` (3 条红) |

顺带记一笔: 实现期第一版把 `cross_zone` 产在 `ack_by` 之前, 与
`STATIC_FACT_IDS` 顺序不符 —— 是 `build_fact_pack` 尾部那条"产出顺序 == 静态清单"
的自检断言当场拦下的, 35 条测试同时红。这条断言不是仪式。

## 六、改动文件清单与建议 commit message

新增:

- `apps/api/alembic/versions/0011_incident_reports.py`
- `apps/api/app/services/report_render.py`
- `apps/api/app/services/report_service.py`
- `apps/api/tests/test_migration_0011.py`
- `apps/api/tests/test_report_render.py`
- `apps/api/tests/test_report_service.py`
- `docs/ai-development/W6-SPEC008-第一段-完成报告.md` (本文件)

修改:

- `mypy.ini` (两个新模块加入严格档白名单同一行)
- `apps/api/tests/conftest.py` / `apps/api/tests/test_agent_helpers.py` /
  `scripts/ops/reset_demo_data.py` (清表清单补 `incident_reports`, 外键所迫, 见第二节)

建议 commit message (git 由本人执行):

```
feat(report): 事故报告第一段 —— 事实包/占位符渲染/两道硬拦与迁移 0011 (SPEC-008)

- incident_reports 表 + partial unique index (一事故一份非弃稿报告);
  agent_tasks 补 task_type CHECK 与 awaiting_review 状态, stage 故意不加约束
- build_fact_pack 纯函数: 事实一律产全 (缺失即"无此记录"), 时区显式传入,
  时间线超 20 条截断并明写条数; 纯度由传递 import 断言守住
- E_BARE_FACT / E_DANGLING_REF 按违规项计数; 字符上限判在渲染后, 渲染前另设 800 硬顶
- 新增测试 48 条; 变异测试 6 项逐一改坏验红; 迁移 downgrade 在两个库上实跑往返;
  五个 CI 脚本全绿
```
