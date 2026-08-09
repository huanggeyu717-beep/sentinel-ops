# W4 第一段 修补 给 CC 的 prompt

第一段已复核通过，**报告与实现一致**，14 条异议全部采纳（其中第 1 条是本段最值钱的
产出，已写进 SPEC-002 的"第一段实现后评审补入"）。下面四处要修，量不大。

依据仍是 `docs/specs/SPEC-002-agent-orchestration.md`，**该文档已更新**，修补涉及的
四处都在里面（第二节判死的兜底分支、第三节 TTL 起算点、第五节裁剪表、
第十二节验收 15b/15c 与变异 22/22b）。开工前先重读那四节，本文件不复述其内容。

文件边界与上一份 prompt 相同。仍然：不接真实 LLM、不写路由与 SSE、不改 `docs/`、
不执行任何 git 命令。

---

## 修补 1（必须改，会真出事）：人回答澄清之后任务被清扫误杀

**症状**：用户隔十分钟回答了 Agent 的问题，任务刚变回 `running`，下一次清扫就把它
判成 `dead_letter`，并顺手把草稿标 `discarded`。

**成因**：`_ANSWER_RESUME` 把 `heartbeat_at` 置成 NULL（对的，原主人早交出租约了），
而 `_REAP_STALE` 的兜底分支拿 `created_at` 当起算点——一条十分钟前建的任务，
一变回 `running` 就已经满足判死条件，**零宽限期**。

**评审方已实测复现**（用的是你那两条 SQL 的原文，灌进真 Postgres 16 跑的）：

```
--- 澄清中:      status=clarifying   已存在时长 00:10:00
--- 人现在回答:   status=running  stage=discovering  heartbeat=NULL
--- 一次清扫:    -> dead_letter / lease_timeout
```

**修法**：按 SPEC-002 第二节新写死的那条规矩落地——**任何让任务变回 `running` 的
写操作都要同时把 `heartbeat_at` 置成 `now()`**。目前是 `_ANSWER_RESUME` 那一处
（`create_task` 那处 `created_at` 与 now 同时，本来就不受影响，但请一并核对）。

兜底分支**保留**，它防的"进程在建任务与认领之间死掉"是真场景；改的是它的起算点
语义——落地之后那个分支只需比对 `heartbeat_at`，不必再看 `created_at`。请顺手把
`_REAP_STALE` 简化成单一判据并更新它的注释，两个起算点并存本身就是这次出错的根。

修法评审方也实测过，两个方向都对：刚回答完跑清扫 `0 rows`；回答后 120 秒仍没人认领
照样 `dead_letter`。

**要补的测试（对应 SPEC-002 验收 15c，两条都要）**：

1. 建任务后从没认领过、超过阈值 → `dead_letter`（这条守的是你补的那个兜底分支
   本来要防的场景，现在一条测试都没有）；
2. **老任务（`created_at` 手工改老到十分钟前）被人回答后刚变回 `running` → 跑一次
   `reap` → 必须还是 `running`**。

**变异（SPEC-002 变异 22b）**：把"变回 running 时打一次卡"那一句去掉 →
上面第 2 条必须红。报告里照旧写"改了哪一行成什么、哪条红"。

### 顺带说明为什么 39 条测试一条都没抓到它

不是你漏测，是两件事叠在一起：`conftest` 把打卡间隔设成 3600（那个处置本身没问题，
死锁的理由成立），所以 `maintenance_loop` 在整套测试里从没真跑过，只有 `beat()` /
`reap()` 被直接调用；而所有 reap 测试都是手工改 `heartbeat_at`，走的都是第一个分支。
**唯一没被测试碰过的那个分支，正好是出问题的那个。** 这条值得写进
`docs/ai-development/defect-log.md`——不过 `docs/` 归评审方，你在报告里写一句就行，
我来落。

## 修补 2：一条断言是空的

`apps/api/tests/test_agent_service.py` 的
`test_reap__stale_heartbeat_becomes_dead_letter` 最后一行：

```python
assert any(r["task_id"] == task["id"] for r in reaped) or task["status"] == "dead_letter"
```

`or` 右边在上面三行已经断言过了，永远为真，**整条断言不可能失败**。它想守的是
"`reap()` 确实把这个任务 id 报了出来"（调用方要靠这个返回值去标 `discarded`），
去掉 `or` 后半截即可。

顺手扫一遍你新增的 39 条里有没有同类写法（`assert A or B` 而 B 在同一个用例里
已经被断言过，或 B 恒真）。这类断言比没有测试更坏——它看起来在守着什么。

## 修补 3：`parsing` 阶段模型越界调工具被静默吞掉

`agent_runtime._round` 的 `parsing` 分支是
`if resp.tool_call is not None and resp.tool_call.tool == "ask_clarification"`，
模型要是在这个阶段调了 `create_policy`，条件不成立、直接往下走进 `discovering`，
既没执行也没人吭声。而 `compiling` / `repairing` 走的是 `_expect_tool`，
越界会抛 `model_protocol_error`。

**同一件事两种口径**。改成 `parsing` 也走 `_expect_tool`，允许集合是
"`ask_clarification` 或不调工具"（后者是这个阶段的正常情况，别把它也判成协议错）。

不是安全问题（工具没被执行，三层闸也还在），是排查口径的一致性：同样是"模型不听话"，
一处报错一处静默，将来查问题的人会以为 `parsing` 阶段模型从没越界过。

补一条测试：打桩在 `parsing` 阶段吐一个 `create_policy` 调用 → 任务落
`failed / model_protocol_error`，且**没有任何策略行被建出来**。

## 修补 4：Trace 时间线要能分清"状态迁移"和"工具调用"

状态迁移现在用 `tool_name='stage_transition'` 这个哨兵值混在 `agent_steps` 里，
能用；但 `agent_service.get_timeline` 把它和真工具调用一样返回 `kind='step'`，
第三段的 Trace UI 会把"进入 validating"画成一次工具调用。

在 `_TIMELINE` 那条 SQL 里把这个哨兵值映射成 `kind='transition'` 即可（一行 CASE），
顺手补一条测试断言时间线里两种 `kind` 分得开。第三段做界面时省一次返工。

---

## 完成报告

沿用上一份的八节格式。这次特别要有的：

- **修补 1 的前后对比数字**：修之前"刚回答完跑一次 reap → 状态变成什么"、
  修之后"→ 状态变成什么"，各带上你跑出它的参数（阈值、created_at 偏移量）。
  这是这次修补唯一能证明"真的修好了"的东西。
- **变异 22b 的结果**：改了哪一行成什么、哪条测试红、红的是不是预期的那条。
- 修补 2 扫查的结果：还有没有同类的空断言，有几条、分别在哪。
