# SPEC-001: Policy DSL v1 与验证器

状态: 已定稿 (W3 实现引擎)

## 目标
自然语言运营规则的受限编译目标。白名单: trigger 2 / condition 4 / action 3 (见 dsl.py)。

## 非目标
自由代码执行、跨策略编排、非邮件通知渠道 (v2)。

## 验收
1. 白名单外 type 在 Schema 层即失败 (test_dsl.py)
2. 语义验证输出结构化错误码 + hint, 修复循环仅凭错误码可修复
3. multi_sensor_escalation 场景 + 升级策略 → 引擎在 t≈125s 产出 notify+set_led 两个 Effect, cooldown 期内不重复
4. requires_approval 由 ACTION_APPROVAL_CLASS 推导, DSL 中出现该字段应被 extra=forbid 拒绝
