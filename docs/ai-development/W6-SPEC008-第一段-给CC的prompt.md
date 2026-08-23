# SPEC-008 第一段 — 给 CC 的 prompt

> 用法(两种都行): 让 CC 直接读本文件, 或把分隔线之间的内容粘给它。
> SPEC 已定稿, 这份**只写 SPEC 里没有的**: 文件边界、易错点指路、交付格式。
> 不复述 SPEC。
>
> **CC 读到这里请注意: 分隔线以下就是你这一轮要执行的任务本身, 不是背景资料。**

---

## 任务

实施 `docs/specs/SPEC-008-incident-report.md` 的**第一段**(见文末"分段实施")。
**只做第一段。** 第二段(接 runtime / HTTP / Studio)不要碰, 一行都不要提前写。

**先完整读 SPEC-008**, 尤其第一、二、三、七节和第十一节 —— 2026-08-23 的评审在这
几节里改了七处(定稿决定 E–K 逐条点名了改哪一节), **不要照记忆里的旧版做**。

## 文件边界(严格, 不要越界建文件)

新增:

| 文件 | 内容 | 能不能碰 IO |
|---|---|---|
| `apps/api/alembic/versions/0011_incident_reports.py` | 迁移 | — |
| `apps/api/app/services/report_render.py` | `build_fact_pack` + 渲染器 + 两道检查 | **一行都不许有** |
| `apps/api/app/services/report_service.py` | `load_incident_facts(conn, incident_id)` | 只有它读库 |
| `apps/api/tests/test_migration_0011.py` | 迁移与约束(照 `test_migration_0010.py` 的写法) | 真库 |
| `apps/api/tests/test_report_render.py` | 纯函数档的全部单元测试与变异测试 | **不许连库** |
| `apps/api/tests/test_report_service.py` | 读库那一半 | 真库 |

**不新建 `packages/` 下的包。** 纯函数放在 `app/services/report_render.py` 里,
纯度由一条**传递 import 断言**守住 —— 照 `evals/tests/test_grader_io_boundary.py`
的做法写一条, 断言 `report_render` 的传递 import 里没有 `asyncpg` / `sqlalchemy` /
`httpx` / `app.db`。**只写注释说"这个模块是纯的"不算数**, 本项目在这上面栽过。

修改(只这两处, 别的文件一律不动):

- `mypy.ini` —— 把 `app.services.report_render,app.services.report_service` 加进
  已开严格档的那个白名单列表(**加进同一行的列表里, 不要另起小节**, 文件顶部注释
  写了理由);
- 无。除上面两个新模块进白名单外, 这一段不该改任何既有文件。**如果你发现必须改,
  停下来在报告里说明为什么**, 不要顺手改了。

## 易错点指路(这几处踩了就要返工)

1. **`agent_tasks.status` 的 CHECK 是匿名约束**(`0001_initial.sql` 内联写的,
   名字由 Postgres 生成)。要加 `awaiting_review` 必须先按名字 drop。
   `0007_policy_lifecycle.py` / `0008_agent_orchestration.py` 里已有
   `_constraint_name()` 辅助函数, **复用它, 不要硬编约束名** —— 硬编的名字在别人
   的机器上可能就不是那个。
2. **`stage` 列没有 CHECK, 而且故意不补。** 迁移注释里要写明这个不对称是故意的
   (SPEC 第七节第 4 条)。`collecting` / `drafting` 两个新阶段名因此不用动迁移。
3. **`E_BARE_FACT` 必须先剔除 `{{...}}` 再检查。** 顺序反了, `{{tl_3}}` 会被自己
   的检查拦下 —— 而"全都拦下来"从跑分上看和"检查很严格"长得一模一样。
4. **两个计数器按违规项累加, 不按轮。** 一轮里三处裸写就加 3。
5. **字符上限判在渲染之后**, 渲染前另有一道 800 的宽松硬顶。两个上限是两件事,
   不要合成一个。
6. **事实包一律产全**: 缺失的那条也要出条目(`value=null` / `text="无此记录"`),
   **不是不给**。这一条错了, 第二段的整个"不许编"就塌了。
7. **`severity` / `resolved_kind` 不进裸写黑名单**(SPEC 第三节判据 3)。
   照字面把它们加进去, 正文里一个"高"字、一个"一般"就会把任务打回。
8. **`resolved_by` 有三个前缀不是两个**: `policy:` / `employee:` / `user:`
   (SPEC-003 决策 4, 去 grep 原文, 不要凭记忆)。`resolve_policy` 还要从
   `policy:{id}@v{n}` 里把 id 和版本号拆出来。
9. **迁移必须写 downgrade, 手写不用 autogenerate**(ADR-006), 并且 downgrade
   要真跑一次验证(`make migrate-down` 然后再 upgrade 回去)。

## 变异测试(SPEC 第十一节, 第一段做 1 / 2 / 4 / 5 / 6 / 7)

**每一条都要真的把实现改坏一次、跑一次、看它红, 然后改回来。** 报告里要写
"改坏的是哪一行、红的是哪条测试"。**推断不算**, 本项目为此栽过九次。

第 5 条要**两个方向都测**:

- 把计数器改成恒返回 1 → 那条"正常输入 → 计数为 0"的断言必须红;
- 只测"该加的时候加了"不测"不该加的时候没加", 计数器写成 `return 1` 照样全绿。

## 交之前必须跑的(五个脚本, 一个都不能少)

```
bash scripts/ci/lint.sh
bash scripts/ci/test-unit.sh
bash scripts/ci/test-api.sh
bash scripts/ci/test-eval-smoke.sh
bash scripts/ci/test-docker.sh
```

**`make test` 只含中间两个, 跑它不算数** —— W5 就是这么本机全绿、推上去 CI 红的。
`test-docker.sh` 会起容器跑迁移, 0011 有问题它才拦得住。

`make lint` 之前先 `make lint-version` 确认本机 ruff 版本与
`requirements-dev.txt` 锁的一致。

## 完成报告

写进 `docs/ai-development/W6-SPEC008-第一段-完成报告.md`, 固定三节:

1. **与 SPEC 不一致的地方** —— 包括你认为 SPEC 写错的。**照字面实现不通的地方要
   停下来上报, 不要自己改口径悄悄绕过去**;
2. **自行新增的分支 / 写操作, 各由哪条测试守着** —— 一个都不许漏, 点名测试函数名;
3. **报数字必须带产生它的配置** —— 例如"XX 条测试"要写清是哪个脚本、哪个目录收上来的。

另外这一段特有的一节:

4. **五个 CI 脚本各自的结果**, 逐个贴退出码。

## 红线

- **git 写操作一律不碰**(`add` / `commit` / `push` / `checkout` 都不行)。
  改完列出改了哪些文件、给出建议的 commit message;
- commit message **不写内部角色与返工过程**(`CLAUDE.md` 有专门一节, log 是公开的);
- 任何临时脚本放 `scripts/dev/`, 不许写进系统临时目录;
- 文档里不用表情符号与装饰性图标。

---
