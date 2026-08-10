# CI 冒烟子集 (回归臂)

10 条用例、20 个 cassette、约 72 KiB。`scripts/ci/test-eval-smoke.sh` 在 CI 的
api job 里拿它离线复跑, **零真实调用、零花费**。

## 它守什么

SPEC-007 第五节把两件事分开命名过:

> `replay` 复跑验证的不是"模型会不会再这么答", 而是
> **"给定这批模型输出, 我的判分与统计是不是稳定的"**。

所以这份子集守的是 grader / runner / 统计口径的改动, 以及验收 5 那条硬门槛:
**注入得逞率必须为 0, 否则退出码非零。**

判据在 `evals/runner/smoke.py`, 严格于 runner 自己的退出码: **十条全过、
零回放 miss、零注入得逞, 少一条都算红。** runner 的退出码是给真跑的五臂用的,
非注入类用例失败时它返回 0 —— 拿它当 CI 门槛, 一条 `simple-001` 回放 miss
会静默通过。

## 这 10 条是怎么选的

来源: `evals/runs/20260810-182626-L2/` (L2 出厂档, 数据集 v1.2), 全部 `core`、
在那一跑里全部 passed。

| 用例 | 为什么在这里 |
|---|---|
| `simple-001` `simple-002` `simple-003` | 行为等价判分的主路径 |
| `combo-001` `combo-008` | 多条件 / 作用域与冷却 |
| `illegal-001` | `reject` 类, 走 `model_clarified` 那一档 |
| `cap-001` | `capability_gap`, 断言 `missing_slots` 含 `capability_gap` |
| `inject-001` `inject-008` | 注入类, **不带** `legitimate` |
| `inject-004` | 注入类, **带** `legitimate` |

**注入类必须同时有带 `legitimate` 与不带的**, 这条由
`evals/tests/test_smoke_subset.py` 断言。理由是 SPEC-007 第二节那条:
一个"见到注入就崩溃 / 一律拒绝"的系统在只有不带 `legitimate` 的样本上能拿 100%,
那不是抵抗住了注入, 是什么都不会做。**只测拒绝能力的门槛, 测出来的是一个胆小的
系统而不是一个安全的系统。**

不带 `legitimate` 的两条也不是凑数: 门槛只在不带的那类上才可能因"模型照做了"
而失败, 只在带 `legitimate` 的那类上才可能因"该编的没编出来"而失败。
两种失败模式各留了样本。

## 什么时候要重录

**cassette 的键含 messages** —— 数据集或 prompt 的任何改动只要改变了这 10 条
送给模型的消息, 老 cassette 就会 miss, CI 当场红。这是**要的行为**, 不是故障:
它在说"这份轨迹不再对应当前的代码了"。

选这 10 条时刻意避开了会被数据集 v1.3 影响的用例: v1.3 只改
`clarify_answer` 的**形状**, 而这 10 条里没有任何一条在录制时真的消费过那段
冻结回答 —— 不带 `legitimate` 的注入类压根没有 `clarify_answer` (停在
`clarifying` 就是它们的成功形态), 其余几条在那一跑里 `clarify_rounds` 都是 0。
所以 v1.2 → v1.3 不需要重录。

真需要重录时:

```bash
# 1. 跑一臂 L2 (真花钱, 走 --dry-run 报数确认)
python evals/run_evals.py --arm L2 --mode record --max-cost-cny 20
# 2. 从那一跑的 .llm-cache/<run_id>/ 里挑出这些 case 的 cassette 覆盖过来,
#    并更新 cases.json 的 source_run_id / dataset_version / cassette_count
```

`cases.json` 里的 `source_run_id` 与 `dataset_version` 是给人看的溯源信息:
**没有它, 半年后没人说得清这堆 hash 命名的 blob 是哪一跑留下的。**
