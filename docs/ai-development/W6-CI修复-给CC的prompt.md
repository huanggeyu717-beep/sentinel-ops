# W6 · CI 修复 (一个文件, 十几行)

main 上 `api` job 的最后一步 `scripts/ci/test-eval-smoke.sh` 红了, 从 `203362b`
(花钱护栏) 开始。**只修这一件, 不要顺手做别的。**

## 病因

评测 runner 按 SPEC-007 第七节走真 HTTP, 每条用例真建一条 Agent 任务, 而
`evals/runner/cli.py:46` 的 `EVAL_USER` 全程是同一个账号 (alex)。
`evals/runner/apiproc.py` 起 API 子进程时传的是 `**os.environ`, **没有任何额度
覆盖**, 于是用生产默认值 —— 每账号每天 3 条。十条用例跑到第 4 条就 429。

配额数的是"建过几条任务", 刻意不回补 (第一段的设计, 是对的), 所以这条路是死的:
**这套默认值下评测永远跑不过 3 条。**

## 要做的

在 `evals/runner/apiproc.py` 起子进程的那个 env 里**显式放开这两项**
(做法与 `apps/api/tests/conftest.py` 里那两行同源, 抄它的理由格式):

- `SENTINEL_LLM_DAILY_BUDGET_CNY`
- `SENTINEL_AGENT_USER_DAILY_TASKS`

注释要写清楚**为什么可以放开**: 护栏的对象是公开演示里的匿名访客, 评测是内部
测量设施, 它自己有成本护栏 (SPEC-007 第六节)。

**不许改 `config.py` 里的默认值。** 默认必须是开着的 —— 一个配错的部署不能因此
没有护栏。

## 验收

```
bash scripts/ci/test-eval-smoke.sh      # 现在会在第 4 条挂; 改完必须绿
```

这一步本身就在 CI 里, **不需要另加测试**。

## 不在这一批里 (记着, 别现在做)

1. runner 分不清三种 429 —— `client.py` 的退避重试把配额 429 当"等一等就有",
   实际等不到。**这次不修**, 但下次撞上时日志里只有一个 `raise_for_status`;
2. `make test` 不含 smoke 与 docker, 本机没有一条命令跑得完 CI 会跑的东西。

## 老规矩

git 不执行; 报告写进 `docs/ai-development/W6-CI修复-完成报告.md`, 三节照旧。
