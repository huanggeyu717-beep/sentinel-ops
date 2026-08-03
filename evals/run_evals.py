"""Eval runner (W5)。

用法: python run_evals.py --arm A2 --dataset datasets/policies_v1.jsonl --samples 1
- 按 arm 组装 Agent 配置 (A0 直出 / A1 +工具 / A2 +验证器修复 / A3 +模拟反馈)
- LLM 调用走 record-replay 层: 请求哈希命中 .llm-cache/ 则零成本回放
- 结果写 eval_runs / eval_results, 附 git sha 与 prompt_version
"""
