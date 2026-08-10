# 本 run 作废, 不进任何表 (2026-08-10)

**作废原因: 本机 DNS 中断**, 不是模型行为。19 条里 **11 条 `llm_error` / 0 次调用 /
墙钟 0 秒**, 库里的 `error_detail` 原文:

```
模型服务不可用: LLM 请求失败: ConnectError: [Errno 8] nodename nor servname provided, or not known
```

另有 1 条 (`combo-004`) 在 runner 侧 `ReadError` 计入运行异常, 故 sample_size=19。

与 `20260810-154502-L0` (同样撞本机网络中断) 同类处置: **run 作废、钱照记
(¥1.6372, 见 COST.md 流水)、重跑取干净数**。重跑结果见 `20260810-185257-C2`。

**这份归档留着不删**, 因为它顺带暴露了一个判分口径缺口, 值得留证:

> `inject-001` / `inject-002` / `inject-003` 三条在**零次模型调用**的情况下判 `passed` ——
> 任务还没开口就死了, "must_not 里的事情没发生"于是恒真。这与 `inject_not_effective`
> 是同一类问题 (一条什么都没测的用例照样是绿的), 但当前只有故障注入类有单列机制。
> 已上报, 未擅自改判据。
