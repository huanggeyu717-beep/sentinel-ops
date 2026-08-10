#!/usr/bin/env python3
"""两组归档的**逐格对照** (W5 第二段重跑修补, 报告第 23 项)。

数据集 v1.1 -> v1.2 补齐 `clarify_answer` 之后 L2/C1/C2 重跑, 必须回答的是
"**哪几格变了、变了多少**", 不是"新表长什么样"。旧表不删, 归档留着备查。

对照三样东西:
1. 分类别成功率逐格 (旧 -> 新, 差值);
2. 每条用例的判分翻转 (失败->通过 / 通过->失败) 与 failure_kind 的变化;
3. 被设施卡死的那批 (旧: 终态 clarifying 且 kind 会产出策略) 逐条的新结果。

用法:
    .venv/bin/python scripts/dev/compare_arm_tables.py \\
        L2=evals/runs/<旧>:evals/runs/<新> C1=<旧>:<新> ...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CATEGORIES = ("simple", "combo", "ambiguous", "illegal", "repairable",
              "capability_gap", "tool_fault", "prompt_injection")
PRODUCTIVE_CATEGORIES = ("simple", "combo", "repairable", "tool_fault")


def load(run_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    rows = {
        str(json.loads(line)["case_id"]): json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
        if line.strip()
    }
    return manifest, rows


def cat_counts(rows: dict[str, dict[str, Any]], category: str) -> tuple[int, int]:
    subset = [r for r in rows.values() if r["category"] == category]
    return sum(1 for r in subset if r["passed"]), len(subset)


def macro(rows: dict[str, dict[str, Any]]) -> float:
    rates = []
    for category in CATEGORIES:
        passed, total = cat_counts(rows, category)
        if total:
            rates.append(passed / total)
    return sum(rates) / len(rates) if rates else 0.0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    pairs = []
    for spec in argv:
        arm, _, dirs = spec.partition("=")
        old_dir, _, new_dir = dirs.partition(":")
        pairs.append((arm, Path(old_dir), Path(new_dir)))

    print("## 分类别成功率逐格对照 (旧 v1.1 -> 新 v1.2)\n")
    header = "| 类别 | " + " | ".join(f"{a} 旧→新 (Δ)" for a, _, _ in pairs) + " |"
    print(header)
    print("|---|" + "---|" * len(pairs))
    loaded = [(arm, load(o), load(n)) for arm, o, n in pairs]
    for category in CATEGORIES:
        cells = []
        for _arm, (_, old_rows), (_, new_rows) in loaded:
            op, ot = cat_counts(old_rows, category)
            np_, nt = cat_counts(new_rows, category)
            if not ot and not nt:
                cells.append("—")
                continue
            delta = np_ - op
            mark = "" if delta == 0 else (f" (**{delta:+d}**)")
            cells.append(f"{op}/{ot} → {np_}/{nt}{mark}")
        print(f"| {category} | " + " | ".join(cells) + " |")
    macro_cells = []
    for _arm, (_, old_rows), (_, new_rows) in loaded:
        om, nm = macro(old_rows), macro(new_rows)
        macro_cells.append(f"{100 * om:.0f}% → {100 * nm:.0f}%"
                           f" (**{100 * (nm - om):+.0f}pt**)")
    print("| **macro** | " + " | ".join(macro_cells) + " |")

    for arm, (old_manifest, old_rows), (new_manifest, new_rows) in loaded:
        print(f"\n## {arm}: {old_manifest['run_id']} -> {new_manifest['run_id']}\n")
        shared = sorted(set(old_rows) & set(new_rows))
        gained = [c for c in shared if not old_rows[c]["passed"] and new_rows[c]["passed"]]
        lost = [c for c in shared if old_rows[c]["passed"] and not new_rows[c]["passed"]]
        print(f"- 比较的用例 {len(shared)} 条 "
              f"(旧独有 {sorted(set(old_rows) - set(new_rows))}; "
              f"新独有 {sorted(set(new_rows) - set(old_rows))})")
        print(f"- **失败 -> 通过 {len(gained)} 条**: {gained}")
        print(f"- **通过 -> 失败 {len(lost)} 条**: {lost}")

        stuck = [
            c for c in shared
            if old_rows[c]["final_status"] == "clarifying"
            and old_rows[c]["category"] in PRODUCTIVE_CATEGORIES
        ]
        print(f"\n### 旧跑里被设施卡死的 {len(stuck)} 条, 逐条的新结果\n")
        print("| 用例 | 旧: 追问的槽位 | 新: 终态 | 新: 判分 | 新: failure_kind |")
        print("|---|---|---|---|---|")
        for case_id in stuck:
            old_slots = sorted({
                s for round_ in old_rows[case_id]["artifact"]["missing_slots"]
                for s in round_
            })
            new = new_rows[case_id]
            print(f"| {case_id} | {', '.join(old_slots) or '—'}"
                  f" | {new['final_status']}"
                  f" | {'**通过**' if new['passed'] else '未通过'}"
                  f" | {new['failure_kind'] or '—'} |")
        if stuck:
            fixed = sum(1 for c in stuck if new_rows[c]["passed"])
            print(f"\n小计: 卡死 {len(stuck)} 条里 **{fixed} 条编对**, "
                  f"{len(stuck) - fixed} 条仍未通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
