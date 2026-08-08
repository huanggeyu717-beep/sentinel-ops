# W3 第二段 — 给 Claude Code 的 prompt

第二段只做「数据库 + service 层 + 引擎接管」，**不做 HTTP 接口**（挪到第三段）。
这样切是为了让这一段能单独复核完：这段最值钱的东西是数据库约束，
它不需要接口就能验，混进路由代码反而看不清。

把分隔线之间的内容复制给 CC。

---

第一段已收尾（54 条测试全绿，三处修补与两处口径跟进都复核通过）。现在做第二段。

## 依据

- `docs/specs/SPEC-006-policy-lifecycle.md` —— **本段的唯一依据，逐条实现**
- `docs/adr/ADR-007-publish-requires-approval-fk.md` —— 通读，理解为什么发布要靠外键
- `docs/specs/SPEC-001-policy-dsl.md` —— 已实现，本段只需查阅，**不要改它对应的代码**
- `docs/adr/ADR-006-alembic-migrations.md` —— 迁移的既定写法

## 本段范围

做：迁移 `0007`（含种子）、`policy_service`、`policy_runtime`、Effect 应用器、
tick 后台任务、删掉 `apply_sensor_state`、SPEC-006 第三节的审批与发布逻辑。

**不做**：HTTP 路由与接口（`app/routers/policies.py` 之类）、`GET /employees`、
Automation Studio 相关的一切。那些是第三段。审批发布的**逻辑**写在 service 层，
本段用直接调 service 的测试来验，不经过 HTTP。

## 文件边界

- 新建：`apps/api/alembic/versions/0007_*.py`、`app/services/policy_service.py`、
  `app/services/policy_runtime.py`、对应测试
- 改：`app/services/incident_service.py`（删 `apply_sensor_state`，拆出只记时间线的
  函数）、`app/services/ingest_service.py`（改调用）、`app/config.py`
  （删 `auto_resolve_dry_seconds`，加 `engine_tick_seconds`）、`app/main.py`
  （tick 任务的启停）、`mypy.ini`（新模块进严格档白名单）
- **不许碰**：`packages/policy_engine/` 与 `packages/scenario/` 的源码（第一段已定稿；
  若你认为里面有真 bug，**先停下来告诉我，不要直接改**）、`apps/web/`、
  `scenarios/`、路由层任何文件

**不要执行任何 git 命令**（`CLAUDE.md` 有）。写完列出改了哪些文件 + 建议的
commit message，我自己敲。

## 几处容易踩的地方

1. **迁移里改 CHECK 约束**：`0001` 建表时那个 status 的 CHECK 是匿名的，
   Postgres 自动命名。要先确认实际约束名再 DROP，不要猜；downgrade 要能原样还回去。
   删 `policies.enabled` 的 downgrade 同理。
2. **域事件的投递必须和状态变更在同一个事务里**（SPEC-006 第四节）。事务回滚则事件
   不存在，引擎看到的与数据库里的永远一致——这是"事件流是引擎唯一输入"能成立的前提。
3. **防递归**：本轮 Effect 全部收集并应用完，新产生的域事件才排进下一轮，
   不许在应用 Effect 的过程中递归调用引擎。第一段的回放模块已经是这个时序，
   两边必须一致，否则模拟对线上没有预测力。
4. **删的是判断逻辑，不是事实记录**。`sensor_still_wet` / `sensor_dry` 两类时间线
   必须保留（SPEC-006 第四节有专门一节），删了 SPEC-003 的验收会当场红。
5. **tick 任务关停要能干净取消**，否则测试会挂住。多实例重复 tick 的已知边界
   要写进代码注释，不能只写在文档里。
6. **CSV 的 zone_id**：第一段发现 `events_from_csv` 产出的事件没有 `zone_id`，
   导致开事故类策略在真实数据上产出恒为 0（SPEC-001 验收 8 的已知限制）。
   service 层能读 `sensors` 表，请在事件规范化时补上 `zone_id`——
   补完之后 CSV 才对全部动作类型可用。补一条测试证明补上了。

## 验收

照 SPEC-006 第七节逐条落测试。**其中第 15 到 20 条最要紧**——那几条是绕过应用层
直接写数据库、看约束拦不拦得住。端到端只证明"正常路径走得通"，这几条证明
"异常路径走不通"，而不变量 1 的分量全在后者。

第 10 到 12 条（禁止自批）本段用直接调 service 的方式验，不经过 HTTP。

## 报告

与前两段相同。特别是「与 SPEC 不一致的地方逐条列出」。

另外这一段请**自己先做一次变异测试再交给我**：把第 15 条那个约束
（`policy_publications.approval_id` 的 NOT NULL 外键）在迁移里去掉，
确认对应测试真的会红，然后恢复。**报告里贴出这次红灯的实际输出。**
理由：那条约束是整个项目的核心主张，"我以为它拦住了"和"我验过它拦得住"是两回事。
