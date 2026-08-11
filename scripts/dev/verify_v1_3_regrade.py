#!/usr/bin/env python3
"""验证 v1.2 与 v1.3 在 A0/A1 上判分逐条相同 —— 相同则 L0/L1 不必重跑。

与 `verify_v1_2_equivalence.py` 同一套手法, 复用它的 `outcome_from_row` /
`regrade` / `check_profiles`, **不另抄一份** (两份走散是本项目最忌讳的事)。
四段可核对的机器证据:

1. **内容层**: 剥掉两版各自的 `clarify_answer` 键后内容 sha256 必须相等 ——
   证明这次只改了那些键的形状, 判据一个字节没动
   (`verify_v1_3_equivalence.py` 是同一条检查的独立入口);
2. **消费点层**: `clarify_answer` 全仓库只有一个读取点
   (`evals/runner/client.drive_case`), 只在状态为 `clarifying` 时用得上;
   A0/A1 的 `AblationProfile.clarification=False`, 本脚本直接断言这两档的 profile,
   并核对 L0/L1 归档里 `clarifying` 与 `clarify_rounds > 0` 的条数为 0;
3. **判分层**: 拿归档 `artifact` 重建 CaseOutcome, 用 v1.2 与 v1.3 两版用例定义
   各离线重判一遍, 逐条比 (passed, failure_kind, intercepted_at, observations);
4. **grader 层 (v1.3 新加)**: 断言 `evals/graders/` 全目录**一次都没有提到
   `clarify_answer`** —— 它是 runner 侧的东西。这一条比前三条更根本:
   判分器压根读不到这个字段, 改它的形状就不可能改变任何一条判分。

零网络零模型零花费。有一条不同就退出码 1 (那就老实重跑)。

用法: .venv/bin/python scripts/dev/verify_v1_3_regrade.py \
          evals/runs/20260810-155429-L0 evals/runs/20260810-155651-L1
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for _p in ("", "apps/api", "packages/policy_engine", "packages/scenario",
           "scripts/dev"):
    sys.path.insert(0, str(REPO / _p))

from verify_v1_2_equivalence import (  # noqa: E402  复用, 不另抄一份
    check_profiles,
    content_sha,
    regrade,
)

from evals.runner.grading import ScenarioEvents  # noqa: E402

DATASET = REPO / "evals/datasets/policies_v1.jsonl"
GRADERS = REPO / "evals/graders"
V12_REF = "HEAD"  # v1.3 还没 commit 时, HEAD 上那份就是 v1.2


def load_v13() -> list[dict[str, Any]]:
    return [json.loads(x) for x in DATASET.read_text().splitlines() if x.strip()]


def load_v12() -> list[dict[str, Any]]:
    text = subprocess.run(
        ["git", "show", f"{V12_REF}:evals/datasets/policies_v1.jsonl"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def without_clarify_answer(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = json.loads(json.dumps(cases))
    for case in out:
        case.get("expected", {}).pop("clarify_answer", None)
    return out


def graders_never_read_clarify_answer() -> list[str]:
    return [
        f.name for f in sorted(GRADERS.glob("*.py"))
        if "clarify_answer" in f.read_text()
    ]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    run_dirs = [Path(a) for a in argv]

    v12, v13 = load_v12(), load_v13()
    sha12 = content_sha(without_clarify_answer(v12))
    sha13 = content_sha(without_clarify_answer(v13))
    shapes12 = {type(c["expected"].get("clarify_answer")).__name__ for c in v12
                if c["expected"].get("clarify_answer") is not None}
    shapes13 = {type(c["expected"].get("clarify_answer")).__name__ for c in v13
                if c["expected"].get("clarify_answer") is not None}

    print("=== 1. 内容层: 剥掉 clarify_answer 后的内容哈希 ===")
    print(f"  v1.2 {sorted(shapes12)} -> v1.3 {sorted(shapes13)}")
    print(f"  sha256:{sha12}  vs  sha256:{sha13}")
    if sha12 != sha13:
        print("  不相等 —— 判据被动过了, 停下")
        return 1
    print("  相等 —— 判据一个字节没动")

    print("\n=== 4. grader 层: graders/ 里有没有提到 clarify_answer ===")
    offenders = graders_never_read_clarify_answer()
    if offenders:
        print(f"  {offenders} 提到了它 —— 前提不成立, 老实重跑")
        return 1
    print("  一个文件都没提到 —— 判分器读不到这个字段, 改它的形状不可能改判分")

    print("\n=== 2. 消费点层: A0/A1 的 profile ===")
    profiles = check_profiles()
    for level, fields in profiles.items():
        print(f"  {level}: {fields}")

    scenarios = ScenarioEvents()
    all_same = True
    report: dict[str, Any] = {}
    for run_dir in run_dirs:
        rows = [json.loads(x) for x in
                (run_dir / "results.jsonl").read_text().splitlines() if x.strip()]
        clarifying = [r["case_id"] for r in rows if r["final_status"] == "clarifying"]
        rounds = [r["case_id"] for r in rows if int(r.get("clarify_rounds") or 0) > 0]
        before, after = regrade(v12, rows, scenarios), regrade(v13, rows, scenarios)
        diff = sorted(k for k in after if before[k] != after[k])
        same = not diff and not clarifying and not rounds
        all_same &= same
        print(f"\n=== 3. 判分层: {run_dir.name} ({len(rows)} 条) ===")
        print(f"  终态 clarifying 的: {len(clarifying)} 条 {clarifying}")
        print(f"  clarify_rounds > 0 的: {len(rounds)} 条 {rounds}")
        print(f"  v1.2 与 v1.3 判分不同的: {len(diff)} 条 {diff}")
        print(f"  {'逐条相同 —— 不必重跑' if same else '有差异 —— 必须重跑'}")
        report[run_dir.name] = {
            "cases": len(rows), "clarifying": clarifying,
            "clarify_rounds_gt_0": rounds, "regrade_diff": diff, "same": same,
        }

    if not all_same:
        return 1
    for run_dir in run_dirs:
        (run_dir / "dataset_v1_3_equivalence.json").write_text(json.dumps({
            "conclusion": "v1.2 与 v1.3 下逐条同分, 未重跑",
            "dataset_sha_without_clarify_answer": sha13,
            "clarify_answer_shape": {"v1.2": sorted(shapes12), "v1.3": sorted(shapes13)},
            "graders_reading_clarify_answer": [],
            "ablation_profiles": profiles,
            **report[run_dir.name],
        }, ensure_ascii=False, indent=2) + "\n")
        print(f"\n已写入 {run_dir / 'dataset_v1_3_equivalence.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
