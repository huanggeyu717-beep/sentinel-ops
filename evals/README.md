# Evals

事实源是 **SPEC-007** (docs/specs/SPEC-007-evals-and-ablation.md), 本文件只放指针,
不复述配比与判据 —— 复述一份就是同一个事实存两份。

- `datasets/` 固定评测集 (结构与字段见 `datasets/README.md`, 配比见 SPEC-007 第二节)
- `graders/` 全部确定性判分, 零 LLM judge (`CLAUDE.md` 工程约定)。过程指标
  (读 `agent_steps`) 不归 grader, 归第二段的消融 runner
- `runs/<run_id>/` 每次评测 run 的归档 (manifest + results + summary);
  评测结果**不进数据库**, 理由见 SPEC-007 第五节末
- `COST.md` 累计花费台账 (第一行是与方舟控制台对账后的 W4 基准)
- 硬门槛: `prompt_injection` 类不足 100% 时退出码非零, CI 红
