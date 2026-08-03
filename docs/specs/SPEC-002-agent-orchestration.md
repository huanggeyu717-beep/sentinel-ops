# SPEC-002: Agent 编排状态机

状态: 草案 (W4 实现)

## 状态机
parsing → discovering → compiling → validating →(invalid)→ repairing(≤2) → simulating
→ awaiting_approval → publishing → completed | failed | rejected | clarifying

## 工具 (v1 锁 9 个, 分级见 CLAUDE.md 不变量 1)
read: list_zones / list_sensors / list_roles / get_available_actions
draft: create_policy_draft   sim: validate_policy / simulate_policy
write: publish_policy (强制 approvals 记录)   终止: ask_clarification

## 预算与恢复
全局 120s / 单工具 10s / 修复 ≤2 次 / 仅对可重试错误指数退避; 超限落 dead_letter 并给出解释。

## 验收
歧义→clarifying; 越权→工具列表内无 publish_policy; 工具超时→重试后成功或干净失败;
每步落 agent_steps 并 SSE 推送。
