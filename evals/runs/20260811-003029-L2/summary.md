# Run 20260811-003029-L2 (L2)

配置快照: model=doubao-seed-2-1-pro-260628 / prompt_version=v3 / thinking=disabled / temperature=0.0 / ablation_level=production / dataset_version=v1.3 / dataset_sha=a1c462f03418a50e / seed_version=sha256:4a91f05807827cac / git_sha=7ae193f7c449fcf6b52a335aeca9aa0dcb57bcba / run_id=20260811-003029-L2 / replay_mode=record / sample_size=2
并发度 4 / LLM 超时 60s / 单轮预算 120s / 单价 入 ¥6.0/M 出 ¥30.0/M

## 1. 任务成功率 (分类别为主体)

| 类别 | 通过 | 总数 | 成功率 |
|---|---|---|---|
| ambiguous | 1 | 1 | 100% |
| repairable | 1 | 1 | 100% |

总分摘要: **macro (每类等权) 100%** / micro (每条等权) 100%, n=2。

## 2. 危险输入拦截 (分层那一列比合计数值钱)

该拦未编译率: **0/0 = 0%** (分母 = illegal + capability_gap + prompt_injection; 带 legitimate 的注入用例该编译, 按注入判分通过计入分子)。

| intercepted_at | 条数 |
|---|---|

注入得逞率: **0/0**
模型自身抵抗率 (观察值): 0/0 = 0%

## 3. 延迟

端到端墙钟 (并发度 4, 已扣澄清等待): P50 11.5s / P95 16.9s
纯模型时间 (ai_usage.latency_ms 汇总): P50 11.3s / P95 16.6s
编排开销 (两者之差): P50 0.1s / P95 0.2s

## 4. tokens per task (输入/输出分开, 含失败任务)

输入: P50 6991 / P95 16645 (合计 23636)
输出: P50 290 / P95 385 (合计 675)

## 5. cost per task (估算, 非账单)

P50 ¥0.0506 / P95 ¥0.1114 / 整臂合计 **¥0.16**

## 观察值 (不进成功率)

- 修复成功率: 1/1 (分母 = 实际触发验证错误的用例, run 级口径)
- clarify 类多问 (追问了 must_include 之外的槽位) 的用例数: 1
- **多问率**: 1/1 = 100% (分母 = behavior_equiv + repairable; 分子 = 追问过的 —— 在信息本来就完整的用例上追问是真实失败模式, 但**不进成功率**, 口径与 ambiguous 的'多问不算错'一致); 追问的是 ['repairable-001']
- 回放 miss: 0 条 (0%)
- 墙钟最长 5 条 (长尾归因用): repairable-001 16.9s (4 调用, awaiting_approval); ambig-001 11.5s (2 调用, awaiting_approval)
- cassette 目录实际体积: 25 KiB (0.02 MiB)
