# CI 修复: test_report_permissions 在全新库上 401 — 给 CC 的 prompt

> 用法: 让 CC 直接读本文件, 或把分隔线之间的内容粘给它。
>
> **CC 读到这里请注意: 分隔线以下就是你这一轮要执行的任务本身, 不是背景资料。**

---

## 症状

CI 的 api job 红一条:

```
FAILED apps/api/tests/test_reports_http.py::test_report_permissions__viewer_reads_but_cannot_write
AssertionError: assert 401 == 403
```

viewer 用 Bearer 头 POST `/incidents/{id}/report`, 期望 403 (已登录但无权限),
实际 401 (未登录 / 凭据无效)。**401 不是权限问题** —— `get_current_user` 里
只有三条路通向 401: 没 token、`decode_token` 抛 InvalidToken、
`get_user(session, user_id)` 返回 None。

## 必读: 已经排除掉的, 不要重走

以下每一条都是**跑出来的**, 不是推断。别再花时间验证它们:

| 试过什么 | 结果 |
|---|---|
| 单跑 `test_reports_http.py` | **绿** (8 passed) |
| 跑整个 `apps/api/tests`, 常驻旧库 | **绿** (368 passed) |
| **先 DROP `sentinel_test` 再跑整个目录** | **红** —— 这是唯一的稳定复现 |
| 排除 `test_prod_seed.py` (唯一 `DELETE FROM users` 的测试) | 仍红 |
| 排除三个 `test_migration_000*.py` (会 downgrade/upgrade) | 仍红 |
| token 过期? | 否, `TOKEN_TTL` 是 8 小时, 观察到的 token `exp - iat = 28800` |
| cookie 抢在 Bearer 前面? (`get_current_user` 里 cookie 优先) | `test_reports_http.py` 里一处 cookie 都没有; `auth_headers` 每次登录后 `client.cookies.clear()` |
| 夹具 TRUNCATE 了 users? | 否, `TELEMETRY_TABLES` 与 `AGENT_TABLES` 都不含 users |

## 已知的两条线索 (还没连起来)

**线索一: 观察到的 token 里 `sub` = 102, 而跑完之后库里 viewer 的 id 是 106。**

```
 id  |       email
   1 | admin@example.com
   2 | chris@example.com
   3 | alex@example.com
 104 | bo@example.com
 105 | dana@example.com
 106 | viewer@example.com
序列 last_value = 106, is_called = t
```

(注意: 102 与 106 来自**两次不同的跑**, 不能直接当成"同一跑里 id 变了"的证据 ——
**这正是要你去测的第一件事**。)

**线索二: 这张表上有两套互相打架的编号规则。**

- **迁移 0007** (`0007_policy_lifecycle.py`): 先
  `setval(pg_get_serial_sequence('users','id'), GREATEST(COALESCE(max(id),0), 100))`
  把序列顶到 ≥100, 再**走序列**插 dana / viewer (→ 101、102);
  它的 `downgrade` 会 `DELETE FROM users WHERE email IN ('dana@example.com','viewer@example.com')`;
- **`app/db.py` 的 `DEV_SEED_SQL` / `ADMIN_SEED_SQL`**: 用**写死的 id** (1/2/3)
  插 admin / chris / alex, 末尾
  `setval(pg_get_serial_sequence('users','id'), GREATEST(max(id), 1))`;
- `conftest.viewer_headers` 与 `test_agent_http.py` 还各有一处
  `INSERT INTO users (email, ...) ON CONFLICT DO NOTHING` —— **走序列, 且即使
  什么都没插也会消耗一个 nextval**。

谁最后跑谁说了算。全新库上这些顺序与常驻库不同, 这是本次红的土壤。

## 你要做的

### 第一步: 先测出机制, 再动手改 (这一步不许跳)

**写一个探针**, 放 `scripts/dev/`, 目标是回答一个问题:
**在同一次失败的跑里, viewer 的 id 变过没有?**

至少要打印: `viewer_headers` 拿到 token 那一刻 viewer 的 id、token 里的 `sub`、
失败断言那一刻库里 viewer 的 id、以及 `get_user(sub)` 是不是 None。
探针要能一条命令跑完 (含 DROP 库), 输出自足。

**在探针跑出结论之前不要提修法。** 评审方这一轮已经猜错两次
(先猜 `test_prod_seed`, 再猜迁移 roundtrip), 两次都是排除法跑出来才知道错的 ——
本项目的规矩是"结论必须是跑出来的", 这一条对病因同样生效。

### 第二步: 修

修法由第一步的结论决定, 但**有两条边界**:

1. **不许靠调整测试顺序、改文件名、或给这条用例加 skip 绕过去。** 它现在是"第一个
   排在迁移测试之后、又用 session 缓存凭据的文件"; 绕过去只是把下一次爆炸推给
   下一个加文件的人;
2. **修在责任方, 不修在受害方。** 如果是"谁改了库、谁负责让缓存失效",
   就修那一侧, 不要简单地把 `viewer_headers` 降成函数级 —— 那会让每条用例多跑一次
   bcrypt (`_BCRYPT_ROUNDS = 12`), 而且**只挡住 viewer 这一个账号**,
   `auth_headers` 缓存的其它账号照旧。除非探针证明那才是正解, 并在报告里论证。

### 第三步: 加一条守它的测试

新增的回归测试必须满足: **把你的修复回退掉, 它会红。** 报告里写清你怎么验证的
(改坏 → 跑 → 红 → 改回)。

**并且它要在常驻库上也能红** —— 只在"刚 DROP 过的库"上才红的测试, 等于把这个坑
留在了本机跑不出来的地方, 与本次事故同形。做不到就在报告里说明为什么。

### 第四步: 补上 `make ci-repro` 的 api 档

这次事故暴露了那笔老债的**完整形状** (原来只记了"`make test` 不含
`test-eval-smoke.sh` 与 `test-docker.sh`"), 实际是三层:

1. `scripts/ci/*.sh` 在本机**故意不装依赖** (`lib.sh` 的 `ci_pip_install` 只在
   `CI=true` 时动手)。没进 venv 就跑, 得到的是
   `ModuleNotFoundError: No module named 'asyncpg'` —— **看起来像代码坏了,
   其实是环境没配好**, 与 `pytest.ini` 里那段注释警告过的坑一模一样, 只是换了个模块名;
2. **本机的 `sentinel_test` 是常驻库, CI 每次是全新库** —— 本次的红就藏在这个差异里;
3. 本机没有任何一条命令跑得完 CI 的全部五个脚本。

加一个 `make ci-api-repro`: 检查 venv (没进就打印一句人话并退出, 不要让 pytest
抛一个看起来像代码坏了的 ImportError) → DROP `sentinel_test` → 跑
`pytest apps/api/tests evals/runner/tests`。
**目标是本机一条命令就能证明 api job 会绿。**

顺带在 `scripts/ci/lib.sh` 里加那道前置检查 (不在 CI 且关键模块 import 不到时,
打印"本机跑请先 source .venv/bin/activate"再退出)。

## 不要碰

SPEC-008 的任何业务逻辑 (`report_render.py` / `report_service.py` /
`report_task_service.py` / `reports.py` / 迁移 0011)。**这次红不是它们的问题** ——
`test_reports_http.py` 只是第一个把这处旧脆弱照出来的文件。
真发现必须动, 停下来在报告里说明。

## 交之前必须跑的

```
bash scripts/ci/lint.sh
bash scripts/ci/test-unit.sh
bash scripts/ci/test-eval-smoke.sh
bash scripts/ci/test-docker.sh
make ci-api-repro          # 你这一轮新加的, 它替代 test-api.sh
python3 scripts/dev/mutate_spec008_stage2.py   # 六条应全红、基线绿, 证明没碰坏 008
```

## 完成报告

写进 `docs/ai-development/W6-CI修复-viewer401-完成报告.md`, 固定三节
(与 SPEC 不一致 / 新增分支与写操作各由哪条测试守着 / 报数字带产生它的配置),
外加:

- **探针的输出原文** (第一步), 与你据此得出的机制;
- **回归测试的改坏/跑红/改回记录**;
- 上面那串命令各自的退出码。

## 红线

- git 写操作一律不碰; commit message 不写内部角色与返工过程;
- 临时脚本放 `scripts/dev/`; 文档不用表情符号与装饰性图标。

---
