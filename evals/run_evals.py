"""Eval runner 入口 (W5 第二段, SPEC-007 第四、五、六节)。

用法 (先 dry-run, 真花钱模式必须显式给出, 详见 evals/runner/cli.py):
    python evals/run_evals.py --arm L0 --mode record --dry-run
    python evals/run_evals.py --arm L0 --mode record --max-cost-cny 5
    python evals/run_evals.py --arm L0 --mode replay --cassette-dir .llm-cache/<run_id>
    python evals/run_evals.py --reset-db

- 能力档由 AblationProfile 穿过同一份 runtime, 不 fork 状态机;
- 结果归档进 evals/runs/<run_id>/ (manifest + results.jsonl + summary.md),
  **不写数据库** —— 0001 建的三张 eval 表已由迁移 0009 删除 (SPEC-007 第五节末)。
"""
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
# 本地包不装进 venv, 与 pytest.ini 的 pythonpath 同一份路径 (record_cassettes 同款)
for _p in (_REPO, _REPO / "packages" / "policy_engine", _REPO / "packages" / "scenario"):
    sys.path.insert(0, str(_p))

from evals.runner.cli import main  # noqa: E402  路径就位后才 import 得到

if __name__ == "__main__":
    sys.exit(main())
