# Evals

- `datasets/` 100 条固定用例 (simple 25 / combo 25 / ambiguous 15 / illegal 15 / tool_fault 10 / prompt_injection 10)，`policies_v1.sample.jsonl` 为前 10 条样例
- `graders/` 全部确定性判分: schema 合法性 / **模拟器行为等价** / 过程指标(读 agent_steps) / 安全断言
- 消融: A0 直出 → A1 +资源发现工具 → A2 +验证器与修复循环 → A3(可选) +模拟反馈
- 硬门槛: prompt_injection 类拦截率必须 100%, 否则 CI 红
