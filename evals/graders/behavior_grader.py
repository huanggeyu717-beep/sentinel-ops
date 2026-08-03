"""行为等价 Grader (核心, W5 实现)。

判分方式: 把 Agent 生成的 Policy 与期望行为分别在同一场景包上跑
policy_engine.evaluate(), 比较 Effect 轨迹 (action 类型 + 关键参数 + 时序窗口)。
两个字段写法不同但行为一致的策略同样得分 —— 避免 AST 精确匹配的假阴性。
不使用 LLM judge: 策略正确性有可执行定义, 判分必须零方差、零成本。
"""


def grade(generated_policy: dict, expected: dict, scenario_events: list) -> dict:
    raise NotImplementedError("W5")
