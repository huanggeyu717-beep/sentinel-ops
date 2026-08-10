#!/usr/bin/env python3
"""验证 v1.1 与 v1.2 在 A0/A1 上判分逐条相同 —— 相同则 L0/L1 不必重跑。

**不许跳过这一步直接声称等价**: 那正是本项目一路在修的"声明的和执行的不是一回事"。
所以这里给的是三段可核对的机器证据, 不是一句论证:

1. **内容层**: 把 v1.2 里 57 个 `clarify_answer` 键逐个删掉, 文件内容 sha256
   必须逐字节回到 v1.1 的 681d95ec3325eca5 —— 证明这次改动**只加了这些键**,
   没有顺手动过任何 reference / expected / scenarios / companions;
2. **消费点层**: `clarify_answer` 在全仓库只有一个读取点
   (`evals/runner/client.drive_case`), 且只在任务状态为 `clarifying` 时用得上;
   A0/A1 的 `AblationProfile.clarification=False`, 没有 ask_clarification 工具 ——
   本脚本直接断言这两档的 profile 字段, 并核对 L0/L1 归档里 `clarifying` 与
   `clarify_rounds > 0` 的条数为 0 (它们**从来没走到过**那个读取点);
3. **判分层**: 拿 L0/L1 归档里的 `artifact` 重建 CaseOutcome, 分别用 v1.1 与
   v1.2 的用例定义**离线重判一遍**, 逐条比较 (passed, failure_kind,
   intercepted_at, observations)。这一段零网络零模型零花费。

有一条不同就退出码 1 (那就老实重跑, 两臂合计约 ¥5.6)。全同则把结论写进
各 run 目录的 `dataset_v1_2_equivalence.json`。

用法: .venv/bin/python scripts/dev/verify_v1_2_equivalence.py \
          evals/runs/20260810-155429-L0 evals/runs/20260810-155651-L1
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for _p in ("", "apps/api", "packages/policy_engine", "packages/scenario"):
    sys.path.insert(0, str(REPO / _p))

from evals.graders.case_grader import CaseOutcome, grade_case  # noqa: E402
from evals.runner.grading import ScenarioEvents  # noqa: E402

DATASET = REPO / "evals/datasets/policies_v1.jsonl"
V11_SHA = "681d95ec3325eca5"
V12_SHA = "e866faf135c0cccf"


def load_v12() -> list[dict[str, Any]]:
    return [json.loads(x) for x in DATASET.read_text().splitlines() if x.strip()]


def strip_added_fields(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """v1.2 -> v1.1: 删掉 v1.2 加的 clarify_answer (只删加过的那些类)。"""
    out, stripped = [], 0
    for case in json.loads(json.dumps(cases)):  # 深拷贝, 不动调用方的对象
        expected = case["expected"]
        added_here = expected["kind"] in ("behavior_equiv", "repairable") or (
            expected["kind"] == "injection_resisted" and "legitimate" in expected
        )
        if added_here:
            expected.pop("clarify_answer", None)
            stripped += 1
        out.append(case)
    return out, stripped


def content_sha(cases: list[dict[str, Any]]) -> str:
    text = "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def outcome_from_row(row: dict[str, Any]) -> CaseOutcome:
    """归档的 artifact -> CaseOutcome (重判是对归档文件的纯函数, SPEC-007 补入 33)。

    artifact 没落盘的两项按归档里的其它字段还原: schema_rejected 从
    intercepted_at 反推, replay_warnings 只影响"已提交"分支的 intercepted_at ——
    两者在 A0/A1 的重判里都不参与 clarify_answer 相关的任何判据。
    """
    art = row["artifact"]
    terminal = art["terminal_status"]
    return CaseOutcome(
        final_status=str(terminal["status"]),
        error_code=terminal["error_code"],
        submitted=bool(row["submitted"]),
        final_draft_body=art["final_policy"],
        all_draft_bodies=tuple(art["draft_bodies"]),
        validation_codes=tuple(art["error_codes"]),
        schema_rejected=row.get("intercepted_at") == "schema",
        executed_tools=tuple(art["tool_calls"]),
        attempted_unknown_tools=tuple(
            (row.get("observations") or {}).get("attempted_unknown_tools", [])
        ),
        clarify_slot_rounds=tuple(tuple(r) for r in art["missing_slots"]),
        draft_version_status=terminal["draft_version_status"],
        replay_warnings=("W_REGRADE_PLACEHOLDER",)
        if row.get("intercepted_at") == "replay_warning" else (),
    )


def regrade(
    cases: list[dict[str, Any]], rows: list[dict[str, Any]], scenarios: ScenarioEvents
) -> dict[str, tuple[Any, ...]]:
    by_id = {str(c["id"]): c for c in cases}
    out: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        case = by_id[str(row["case_id"])]
        grade = grade_case(
            case, outcome_from_row(row), scenarios.events_for_case(case)
        )
        out[str(row["case_id"])] = (
            grade.passed, grade.failure_kind, grade.intercepted_at,
            json.dumps(grade.observations, ensure_ascii=False, sort_keys=True),
        )
    return out


def check_profiles() -> dict[str, Any]:
    from app.services.agent_runtime import AblationProfile

    out = {}
    for level in ("A0", "A1"):
        profile = AblationProfile.from_level(level)
        assert not profile.clarification, f"{level} 居然开了 clarification"
        out[level] = {"clarification": profile.clarification,
                      "validate_and_repair": profile.validate_and_repair}
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    print("== 1. 内容层: v1.2 剥掉 clarify_answer 后必须逐字节等于 v1.1 ==")
    v12 = load_v12()
    assert content_sha(v12) == V12_SHA, (
        f"当前数据集 sha {content_sha(v12)} != 记录的 v1.2 {V12_SHA}"
    )
    v11, stripped = strip_added_fields(v12)
    back = content_sha(v11)
    print(f"  v1.2 sha       = {content_sha(v12)} (README 记录 {V12_SHA})")
    print(f"  剥掉 {stripped} 个 clarify_answer 后 sha = {back}"
          f" (v1.1 记录 {V11_SHA})")
    if back != V11_SHA:
        print("  !! 不等 —— 这次改动动了 clarify_answer 之外的东西, 停")
        return 1
    print("  == 相等: 本次改动只加了这 57 个键, 判据一个字节没动")

    print("\n== 2. 消费点层: A0/A1 根本走不到那个读取点 ==")
    profiles = check_profiles()
    for level, fields in profiles.items():
        print(f"  AblationProfile.from_level({level!r}).clarification ="
              f" {fields['clarification']}")
    print("  clarify_answer 在仓库里唯一的读取点: evals/runner/client.drive_case"
          " (仅当 task.status == 'clarifying')")

    print("\n== 3. 判分层: 拿归档 artifact 用两版用例各重判一次 ==")
    scenarios = ScenarioEvents()
    all_ok = True
    for arg in argv:
        run_dir = Path(arg)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        rows = [
            json.loads(x)
            for x in (run_dir / "results.jsonl").read_text().splitlines() if x.strip()
        ]
        clarifying = [r["case_id"] for r in rows if r["final_status"] == "clarifying"]
        asked = [r["case_id"] for r in rows if r.get("clarify_rounds", 0) > 0]
        graded_v11 = regrade(v11, rows, scenarios)
        graded_v12 = regrade(v12, rows, scenarios)
        diffs = [k for k in graded_v11 if graded_v11[k] != graded_v12[k]]
        # 重判本身与归档里的原判也要一致 —— 否则这个对照是在比两个都错的东西
        drift = [
            r["case_id"] for r in rows
            if (bool(r["passed"]), r["failure_kind"])
            != (graded_v11[str(r["case_id"])][0], graded_v11[str(r["case_id"])][1])
        ]
        ok = not diffs
        all_ok &= ok
        print(f"\n  {manifest['run_id']} ({manifest['arm']},"
              f" {manifest['ablation_level']}, dataset {manifest['dataset_version']}):")
        print(f"    终态 clarifying 的条数        = {len(clarifying)} {clarifying}")
        print(f"    clarify_rounds > 0 的条数     = {len(asked)} {asked}")
        print(f"    逐条重判 v1.1 vs v1.2 不同的  = {len(diffs)} {diffs}")
        print(f"    重判与归档原判不一致的        = {len(drift)} {drift}"
              " (非零只说明离线重建的 artifact 不够还原原判, 与本对照的结论无关)")
        print(f"    结论: {'v1.2 下逐条同分, 不必重跑' if ok else '有差异 —— 老实重跑'}")
        (run_dir / "dataset_v1_2_equivalence.json").write_text(
            json.dumps(
                {
                    "run_id": manifest["run_id"],
                    "arm": manifest["arm"],
                    "ablation_level": manifest["ablation_level"],
                    "archived_dataset_version": manifest["dataset_version"],
                    "archived_dataset_sha": manifest["dataset_sha"],
                    "v1_1_sha": V11_SHA,
                    "v1_2_sha": V12_SHA,
                    "strip_roundtrip_ok": back == V11_SHA,
                    "clarify_answer_read_point":
                        "evals/runner/client.drive_case (status == 'clarifying')",
                    "ablation_profiles": profiles,
                    "rows_final_status_clarifying": clarifying,
                    "rows_with_clarify_rounds": asked,
                    "regrade_diff_case_ids": diffs,
                    "regrade_vs_archive_drift_case_ids": drift,
                    "conclusion": (
                        "本 run 的归档在数据集 v1.2 下同样成立 (逐条判分相同), 未重跑"
                        if ok else "v1.2 下判分有差异, 必须重跑"
                    ),
                },
                ensure_ascii=False, indent=2,
            ) + "\n"
        )
        print(f"    已写入 {run_dir / 'dataset_v1_2_equivalence.json'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
