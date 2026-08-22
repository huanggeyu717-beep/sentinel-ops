# W6 · CI 修复 · 完成报告

> 范围: 只有一件 —— `evals/runner/apiproc.py` 起 API 子进程的 env 里显式放开
> 两项配额, 让评测冒烟不再被公开演示的花钱护栏 (SPEC-009) 打到 429。
> 零真实模型调用, 零花费 (replay 模式)。
>
> 改动文件: 仅 `evals/runner/apiproc.py` (两行 env + 四行注释)。
> `config.py` 默认值、runner、smoke 判据、CI 脚本均未动。

## 一、与 prompt 的出入 (零偏离, 两处报备)

1. **放开的位置在 `os.environ` 之后、`self._overrides` 之前**: 两行写死在
   env 字典字面量里, 排在调用方 overrides 前面 —— 评测五臂正常拿到放开值,
   而将来若有哪个臂要专门测护栏, 传 overrides 仍能压回小值, 不用改这个文件。
2. **本机跑验收脚本时把 `.venv/bin` 挂进了 PATH**: 脚本第 28 行用裸 `python`,
   本机 (macOS) PATH 里没有这个名字, 直接跑在起 API 之前就挂 (127)。CI 的
   runner 镜像里有, 所以这只是本机执行方式, 不是脚本缺陷, 脚本一字未动。

顺带看见但**没动**的一处: `apps/api/tests/conftest.py:35` 注释写生产默认
"每账号 5 条", 而 `config.py:84` 实际默认是 3 —— 陈旧注释, 按"只修这一件"
留给下一批。

## 二、新增/改动各由什么守着

| 改动 | 守着它的东西 |
|---|---|
| env 里两项配额放开 (各 100000) | `test-eval-smoke.sh` 本身: 改前第 4 条 429, 改后十条全过 —— 这个脚本就在 CI 的 api job 里, 回归即红 |
| "为什么可以放开"的理由 | 注释四行写明: 护栏对象是公开演示的匿名访客, 评测是内部测量设施, 自带成本护栏 (SPEC-007 第六节); 与 `conftest.py:35-39` 那两行同源同格式, 互为对照 |
| 生产默认不因此松动 | `config.py` 一字未动 (¥3/天、每账号 3 条仍是默认); 放开只活在评测子进程的 env 里, 不落任何配置文件 |

## 三、验证结果 (数字与产生它的配置)

- **验收命令**: `bash scripts/ci/test-eval-smoke.sh` (本机, `.venv/bin` 挂进
  PATH; Postgres 5433, 评测库 sentinel_eval, 库版本推到 0010_deploy_guardrails)。
- **结果**: 完成 10/10 条, 判据全过 —— "冒烟回归通过: 10 条全过, 零回放 miss,
  零注入得逞"。改前该脚本按病因会在第 4 条撞 429 (同一账号 alex, 默认每天 3 条)。
- **花费**: replay 模式零真实调用, 实际花费 ¥0.00, cassette 命中 20 次调用
  (tokens 68,690/2,273 全部来自回放)。
- prompt 预留的两件 (runner 分不清三种 429 / `make test` 覆盖不了 CI) 均未碰。

## 建议的 commit message

```
fix(evals): 评测 API 子进程显式放开花钱护栏配额, 冒烟不再第 4 条 429

- SPEC-009 护栏默认每账号每天 3 条, 评测十条用例共用一个账号, 这条路必死
- 放开只在 evals/runner/apiproc.py 的子进程 env 里, config.py 默认值不动
- 验证: scripts/ci/test-eval-smoke.sh 从第 4 条挂到十条全过, replay 零花费
```
