# W6 CI 修复 · viewer 401 · 完成报告

**病因是跑出来的, 不是猜的**: 探针在同一次失败的跑里录到 viewer 的 id 从 102 变成
106, 变化发生在 `test_policy_constraints.py::test_migration_0007__downgrade_then_upgrade_roundtrip`
执行期间 —— 迁移 0007 的往返降到 0006 时删掉 dana/viewer, 升回时按序列重插。
session 级缓存的 token 里存的是数字 id (sub=102), 人还在、id 变了, `get_user(102)`
返回 None, 于是 401。修在责任方: 往返测试用新夹具 `preserve_users` 把删掉的行按
**原 id** 复原。全新库全量跑 (make ci-api-repro) 从 1 failed 转绿。

改动清单:

```
apps/api/tests/conftest.py               preserve_users 夹具 (快照/按原 id 复原)
apps/api/tests/test_policy_constraints.py  0007 往返包进 preserve_users + id 断言; 新增回归测试
scripts/ci/lib.sh                        require_modules_outside_ci 前置检查
scripts/ci/test-api.sh                   调 require_modules_outside_ci asyncpg fastapi pytest
scripts/ci/test-unit.sh                  同上 (pytest pydantic)
scripts/ci/test-eval-smoke.sh            同上 (asyncpg httpx)
scripts/dev/ci-api-repro.sh              新增: venv 检查 -> DROP sentinel_test -> test-api.sh
Makefile                                 新增 make ci-api-repro 目标
scripts/dev/probe_viewer401.py           探针驱动 (DROP 库 + 全量跑 + 打印日志)
scripts/dev/probe_viewer401_plugin.py    探针插件 (只旁观不干预)
```

---

## 一、探针输出原文与机制 (第一步)

探针跑法: `python scripts/dev/probe_viewer401.py` (一条命令, 含 DROP `sentinel_test`,
之后全量跑 `apps/api/tests`, 插件只旁观: 包一层 `create_token` / `get_user`,
每条用例前后查一次 viewer 的 id)。

第一次跑探针时插件自己踩了坑: `create_token` 包装里的 `asyncio.run` 在应用事件循环里
被调, 把一批登录打炸 (42 failed / 68 errors), 污染了观测对象。把查询挪进独立线程后
重跑, 得到与 CI **完全一致**的失败形状 (1 failed, 367 passed), 日志原文如下
(签发 token 的重复行略去, 每账号留一条):

```
[viewer id 变化] <未查过> -> <查询失败: UndefinedTableError> (发现于 test_agent_ablation.py::test_profile_production__... 之前)
[viewer id 变化] <查询失败: UndefinedTableError> -> 102 (发生在 test_agent_ablation.py::test_profile_production__... 执行期间)
[签发 token] sub=3 email=alex@example.com 此刻 viewer id=102 (用例 test_agent_http.py::test_post_returns_immediately__stub_deliberately_slow)
[签发 token] sub=102 email=viewer@example.com 此刻 viewer id=102 (用例 test_agent_http.py::test_permissions__viewer_reads_but_cannot_submit)
[签发 token] sub=104 email=bo@example.com 此刻 viewer id=102 (用例 test_agent_http.py::test_reply__non_owner_403_and_task_untouched)
[签发 token] sub=2 email=chris@example.com 此刻 viewer id=102 (用例 test_auth.py::test_login__sets_httponly_lax_cookie_and_body_has_no_token)
[签发 token] sub=1 email=admin@example.com 此刻 viewer id=102 (用例 test_auth.py::test_expired_token__401)
[签发 token] sub=101 email=dana@example.com 此刻 viewer id=102 (用例 test_policies_http.py::test_mainline_b__manager_self_approval_403_then_second_manager_approves)
[viewer id 变化] 102 -> 106 (发生在 test_policy_constraints.py::test_migration_0007__downgrade_then_upgrade_roundtrip 执行期间)
[get_user 返回 None] 查的 sub=102, 此刻库里 viewer id=106 (用例 test_reports_http.py::test_report_permissions__viewer_reads_but_cannot_write)
[收尾] users 全表: 1=admin@example.com, 2=chris@example.com, 3=alex@example.com, 104=bo@example.com, 105=dana@example.com, 106=viewer@example.com
[收尾] users_id_seq last_value=106 is_called=True pytest 退出码=1
```

任务书第一步那个问题的答案: **变过 —— 同一次失败的跑里, viewer 的 id 从 102 变成
106**, 四项观测逐一对上:

- token 签出那一刻: viewer id=102, token sub=102 (失败断言里那个 Bearer 解出来的
  `"sub": "102"` 与探针记录一致);
- 变化点: `test_policy_constraints.py` 的 0007 往返 —— downgrade 到 0006 执行
  `DELETE FROM users WHERE email IN ('dana...', 'viewer...')`, upgrade 先
  `setval(GREATEST(max(id), 100))` 再走序列重插;
- 失败断言那一刻: 库里 viewer id=106, `get_user(102)` 返回 None —— 401 的三条路里
  走的是第三条;
- **为什么只在全新库上红**: 全新库上 viewer=102 不是最大 id (test_agent_http 后插的
  bo=104 在它后面), 删掉重插必换号 (max=104 -> setval 104 -> dana=105, viewer=106);
  常驻库上 id 已经收敛到 105/106 这个不动点 (viewer 恰好是最大 id), 删掉重插落回
  原号, "没复原"与"复原了"长得一模一样。一次全新库的跑就把 id 推到不动点, 所以
  本机常驻库怎么跑都是绿的。

评审方排除过的怀疑对象由此洗清: 三个 `test_migration_00*.py` 只在 0008 及以上
往返 (不跨 0007, 不碰 users), `test_prod_seed` 删的是 admin —— 真正的往返住在
`test_policy_constraints.py` 里, 文件名不带 migration。它 docstring 里那句
"本套测试不缓存这两个账号的 token"在 conftest 的 `viewer_headers` 变成 session 级
缓存后就过时了 —— 又一例"声明的和执行的不是一回事"。

## 二、修法与理由 (第二步)

**修在责任方**: 谁删了行, 谁负责把世界还原成拿快照时的样子。conftest 新增
`preserve_users` 夹具 —— 进入时快照 `users` / `user_roles` / `users_id_seq`,
退出时把 "email 还在但 id 变了" 的行删掉重插为**原 id + 原角色**, 序列拨回快照值。
0007 往返测试把 downgrade/upgrade 包进它, 升回后断言 dana/viewer 的 id 与往返前
逐一相同。

没有走的两条路, 按任务书边界说明:

- 没有把 `viewer_headers` 降成函数级: 每条用例多付一次 bcrypt (cost 12), 且只护住
  viewer 一个账号, `auth_headers` 缓存的其它账号照旧裸奔;
- 没有动迁移 0007 本身: downgrade 删掉自己种的账号是正确的还原, upgrade 也不可能
  知道"上次的 id" —— 状态恢复是测试的责任, 不是迁移的。

## 三、新增分支与写操作各由哪条测试守着 (第三步)

| 新增的东西 | 守它的测试 |
|---|---|
| `preserve_users` 的复原逻辑 (删新行/按原 id 重插/角色/序列) | `test_cached_token_survives_0007_roundtrip__with_higher_id_user_present` (新增) |
| 0007 往返不再改变种子账号 id | 同上 + `test_migration_0007__downgrade_then_upgrade_roundtrip` 里新增的 `ids_before` 断言 |
| 往返后缓存 token 仍有效 (受害现场本身) | 新回归测试末尾的 `GET /auth/me` == 200 断言 |
| `require_modules_outside_ci` / ci-api-repro 的 venv 检查 | 无自动测试 (shell 前置检查); 手工验证见第五节 |

**回归测试为什么要自带一个高位 id 用户**: 上一段说的不动点意味着 "在常驻库上跑一次
往返、断言 id 不变" 是恒真的 —— 缺陷在但断言照样绿。测试先走序列插一个 id 更大的
临时用户把巧合破坏掉, 再跑同一段往返; 这样复原逻辑一坏, 常驻库与全新库上它都红,
不会把坑留在"本机跑不出来的地方"。

**改坏 / 跑红 / 改回记录** (任务书第三步要求):

1. 改坏: 把 `preserve_users` 的 `finally: run(restore, snap)` 换成 `pass`;
2. 跑红 (常驻库, 未 DROP): `pytest apps/api/tests/test_policy_constraints.py` →
   **1 failed, 7 passed**, 红的正是新回归测试:

   ```
   >  assert db(seed_user_ids) == ids_before
   E  AssertionError: assert {'dana@example.com': 113, 'viewer@example.com': 114}
                          == {'dana@example.com': 109, 'viewer@example.com': 110}
   ```

   同一跑里旧的 0007 往返用例**仍然绿** —— 常驻库不动点把它的 id 断言变成恒真,
   正是需要专设回归测试的理由的现场版;
3. 改回: 恢复那一行, 同文件重跑 → **8 passed**。

## 四、`make ci-api-repro` 与三层环境差异 (第四步)

这次事故暴露的债是三层, 逐层对应的处置:

1. **脚本在本机故意不装依赖** → `lib.sh` 新增 `require_modules_outside_ci`:
   不在 CI 且关键模块 import 不到时打印
   `本机跑请先 source .venv/bin/activate (当前 python import 不到 asyncpg)` 退出 1,
   不再让 pytest 抛一个看起来像代码坏了的 ImportError。挂进 test-api / test-unit /
   test-eval-smoke 三个脚本 (test-docker 不跑 python, lint.sh 的失败信息本来可读);
2. **本机常驻库 vs CI 全新库** → `make ci-api-repro`
   (scripts/dev/ci-api-repro.sh): venv 检查 → `DROP DATABASE sentinel_test WITH
   (FORCE)` → 跑与 CI 完全同一份 `scripts/ci/test-api.sh`
   (即 `pytest apps/api/tests evals/runner/tests`);
3. **本机没有一条命令跑得完五个 CI 脚本** → 现在
   `lint / test-unit / ci-api-repro / test-eval-smoke / test-docker` 五个都能本机
   单命令跑, 本轮全部跑过, 退出码见第六节。

## 五、报数字带产生它的配置 (第五步)

- 探针数字 (102/104/105/106, 1 failed/367 passed) 的产生配置:
  `python scripts/dev/probe_viewer401.py`, 库
  `postgresql://sentinel:sentinel@localhost:5433/sentinel_test` (先 DROP),
  Python 3.12 venv, pytest 按 `pytest.ini` 从仓库根跑全量 `apps/api/tests`;
- 回归测试红时的 109/110 -> 113/114 产生于**常驻库** (未 DROP, 序列已走到 112),
  同一断言在全新库上会是别的数字 —— 数字依赖序列现状, 断言只依赖 "相等";
- venv 前置检查的验证配置: `env -u VIRTUAL_ENV PATH=/usr/bin:/bin bash
  scripts/ci/test-api.sh` 与 `... scripts/dev/ci-api-repro.sh`, 都打印那句人话并
  退出 1 (macOS 系统环境无 asyncpg);
- CI=true 时 `require_modules_outside_ci` 直接返回, 不影响 CI 现行为
  (依赖仍由 `ci_pip_install` 现装)。

## 六、必跑清单退出码

| 命令 | 结果 | 退出码 |
|---|---|---|
| `bash scripts/ci/lint.sh` | ruff + mypy 全过 (159 files) | 0 |
| `bash scripts/ci/test-unit.sh` | 299 passed | 0 |
| `make ci-api-repro` (新增, 替代 test-api.sh) | DROP 后全新库 **412 passed, 1 skipped** (含原红的 `test_report_permissions__viewer_reads_but_cannot_write` 与两条新测试) | 0 |
| `bash scripts/ci/test-eval-smoke.sh` | 10 条全过, 零回放 miss, 零注入得逞 | 0 |
| `bash scripts/ci/test-docker.sh` | docker job 全部断言通过 | 0 |
| `python3 scripts/dev/mutate_spec008_stage2.py` | **六条全红 (B1/B1b/B2/B3/B4/B5 各退出码 1), 还原后基线绿** —— 本轮没碰坏 SPEC-008; 跑完 `git diff` 里 `report_task_service.py` / `budget_service.py` 零改动 (真还原了) | 0 |

## 七、与任务书不一致的地方

- 任务书说探针要打印 "失败断言那一刻库里 viewer 的 id、以及 get_user(sub) 是不是
  None" —— 插件是在 `get_user` 返回 None 的**每一次**现场记录的 (整个跑里恰好只有
  失败那一条用例触发), 语义等价, 记录点更准;
- 探针第一版有观测副作用 (asyncio.run 在事件循环里), 修正后才拿到干净复现 ——
  两次跑的日志都在第一节交代了, 没有只报好看的那次;
- 除任务书点名的文件外多改了 `test-unit.sh` / `test-eval-smoke.sh` 各一行
  (挂前置检查): 任务书第四步只点名 test-api 档, 但第 1 层债的描述是对
  `scripts/ci/*.sh` 整体说的, 这两个脚本同样会在 venv 外报 ImportError。
  越界只有这两行, 都是加检查不改行为;
- "不要碰" 清单 (SPEC-008 业务逻辑) 一个字没动 —— `report_task_service.py` 在
  git status 里短暂显示 modified 是变异架子跑动时的中间态, 跑完已还原
  (`git diff` 干净, 见第六节变异行)。
