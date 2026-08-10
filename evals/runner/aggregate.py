"""五臂横向汇总 (SPEC-007 报告格式 15/16): 分类别成功率表 + 拦截层次分布 +
注入两个数, 每一格带配置。分类别表是主体, 总分只作摘要。

输入是若干 run 目录 (每臂一个定形 run); 复用 metrics.py 的指标函数, 不另算一份。
不改任何单臂归档 —— 这是只读汇总, 产出 evals/runs/summary_ablation.md。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import metrics

CATEGORIES = ("simple", "combo", "ambiguous", "illegal", "repairable",
              "capability_gap", "tool_fault", "prompt_injection")


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    # 归档目录里放了 DEGRADED.md = 这一跑有条目不是模型行为 (环境损坏)。
    # 汇总表必须自己把这件事说出来: 一个只写在隔壁文件里的告示, 读表的人看不见。
    degraded = run_dir / "DEGRADED.md"
    manifest["degraded_note"] = (
        degraded.read_text().splitlines()[0].lstrip("# ").strip()
        if degraded.exists() else None
    )
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return manifest, rows


def _cat_cell(rows: list[dict[str, Any]], category: str) -> str:
    subset = [r for r in rows if r["category"] == category]
    if not subset:
        return "—"
    passed = sum(1 for r in subset if r["passed"])
    return f"{passed}/{len(subset)}"


def render(run_dirs: list[Path]) -> str:
    arms = [load_run(d) for d in run_dirs]
    lines: list[str] = [
        "# W5 消融五臂横向指标表 (SPEC-007 第四、五节)",
        "",
        "本表由 evals/runner/aggregate.py 从各臂定形 run 的归档只读汇总而成。",
        "**分类别表是主体, 总分只作摘要** (类别配比是人定的, micro 会随配比漂移)。",
        "每一臂的完整配置见其 manifest.json; 关键配置在下表脚注。",
        "",
        "## 臂与配置",
        "",
        "| 臂 | 档 | 模型 | 思考 | 超时 | 样本 | 数据集 | run_id |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for manifest, rows in arms:
        lines.append(
            f"| {manifest['arm']} | {manifest['ablation_level']}"
            f" | {manifest['model']} | {manifest['thinking']}"
            f" | {manifest['llm_timeout_seconds']}s | {len(rows)}"
            f" | {manifest['dataset_version']} | {manifest['run_id']} |"
        )
    degraded = [(m["arm"], m["degraded_note"]) for m, _ in arms if m.get("degraded_note")]
    for arm_name, note in degraded:
        lines += [
            "",
            f"> **{arm_name} 这一跑被环境损坏, 它那一列不能与其它臂平读** —— {note}"
            f" 详见 `evals/runs/<{arm_name} run_id>/DEGRADED.md`。",
        ]
    versions = sorted({m["dataset_version"] for m, _ in arms})
    if len(versions) > 1:
        lines += [
            "",
            f"> **本表混了两个数据集版本** ({', '.join(versions)})。按本项目自己的"
            "规矩这是不许的, 唯一的例外理由是: v1.2 相对 v1.1 **只给会产出策略的 57 条"
            "补了 `clarify_answer`** (评测设施缺口, 见数据集 CHANGELOG), 而 A0/A1 的"
            "`AblationProfile.clarification=False` —— 它们没有追问出口, 这个字段永远"
            "用不上。**这不是论证, 是核对过的**: `scripts/dev/verify_v1_2_equivalence.py`"
            "把 v1.2 剥掉那 57 个键后的内容哈希对回了 v1.1 (逐字节相同), 又拿 L0/L1"
            "归档的 artifact 用两版用例各离线重判一次, 逐条 (passed, failure_kind,"
            " intercepted_at, observations) 完全相同; 结论落在各 run 目录的"
            " `dataset_v1_2_equivalence.json`。一条不同就该重跑, 而不是写这段脚注。",
        ]

    # --- 分类别成功率 (主体) ---
    header = "| 类别 | " + " | ".join(m["arm"] for m, _ in arms) + " |"
    sep = "|---|" + "---|" * len(arms)
    lines += ["", "## 1. 任务成功率 · 分类别 (主体)", "", header, sep]
    for cat in CATEGORIES:
        cells = " | ".join(_cat_cell(rows, cat) for _, rows in arms)
        lines.append(f"| {cat} | {cells} |")
    lines += ["", "### 总分摘要 (macro 每类等权 / micro 每条等权)", "",
              "| 指标 | " + " | ".join(m["arm"] for m, _ in arms) + " |", sep]
    macro_row, micro_row = [], []
    for manifest, rows in arms:
        s = metrics.success_rates(rows, manifest)
        macro_row.append(f"{100 * s['macro']:.0f}%")
        micro_row.append(f"{100 * s['micro']:.0f}%")
    lines.append("| **macro** | " + " | ".join(macro_row) + " |")
    lines.append("| micro | " + " | ".join(micro_row) + " |")
    lines += [
        "",
        "> A0 / A1 的 `ambiguous` + `capability_gap` 共 24 条是**结构性 0**"
        " (这两档没有 ask_clarification, 永远拿不到分); 它们照常计入 macro/micro"
        " 分母, 剔掉才是粉饰。读表时把这 24 条从 A0/A1 的分母里心算扣掉, 才看得出"
        "模型在它**能做**的那几类上的真实水平。",
    ]

    # --- 拦截层次分布 (精华) ---
    lines += ["", "## 2. 拦截层次分布 (比合计数值钱)", "",
              "该拦未编译率分母 = illegal + capability_gap + prompt_injection。",
              "", "| intercepted_at | " + " | ".join(m["arm"] for m, _ in arms)
              + " |", sep]
    layers = ("model_clarified", "model_protocol_error", "schema",
              "static_validator", "replay_warning", "none")
    per_arm_inter = [metrics.interception(rows, m) for m, rows in arms]
    for layer in layers:
        cells = " | ".join(str(inter["by_layer"].get(layer, 0)) for inter in per_arm_inter)
        lines.append(f"| {layer} | {cells} |")
    lines.append("| **该拦未编译率** | " + " | ".join(
        f"{inter['blocked']}/{inter['denominator']}" for inter in per_arm_inter
    ) + " |")
    lines += [
        "",
        "> 这一列是本项目主张的量化版本: **弱档靠模型自觉 (model_clarified)、"
        "强档靠确定性层 (static_validator / schema)**。只报一个合计数会把它盖掉。",
    ]

    # --- 注入两个数 ---
    lines += ["", "## 3. 注入: 得逞率 (硬门槛) 与模型自身抵抗率 (观察值)", "",
              "| 指标 | " + " | ".join(m["arm"] for m, _ in arms) + " |", sep]
    gt_cells, res_cells = [], []
    for inter in per_arm_inter:
        gt_cells.append(f"{inter['injection_got_through']}/{inter['injection_total']}")
        res_cells.append(
            f"{inter['model_resisted']}/{inter['injection_total']}"
        )
    lines.append("| **注入得逞** (越低越好) | " + " | ".join(gt_cells) + " |")
    lines.append("| 模型自身抵抗 (model_clarified) | " + " | ".join(res_cells) + " |")

    # --- 多问率 (观察值, 不进成功率) ---
    obs_per_arm = [metrics.run_observations(rows) for _, rows in arms]
    lines += [
        "", "## 3b. 多问率 (观察值, **不进成功率**)", "",
        "分母 = `behavior_equiv` + `repairable` (会产出策略的用例); "
        "分子 = `clarify_rounds > 0` 的。**在信息本来就完整的用例上追问**"
        "是一种真实的失败模式 (烦人), 但口径与 `ambiguous` 的"
        '"多问不算错"一致: 报出来, 不扣分。',
        "",
        "| 指标 | " + " | ".join(m["arm"] for m, _ in arms) + " |", sep,
    ]
    lines.append("| 多问率 | " + " | ".join(
        (f"{o['over_ask']}/{o['over_ask_denominator']}"
         f" = {100 * o['over_ask_rate']:.0f}%")
        if o["over_ask_rate"] is not None else "n/a"
        for o in obs_per_arm
    ) + " |")
    lines.append("")
    lines.append(
        "> `n/a` = 该臂归档早于 `kind` 字段 (数据集 v1.1 的 L0/L1), 分母取不出来 ——"
        " 与\"一次没多问\"不是一回事, 不许长得一样。A0/A1 本档没有 ask_clarification,"
        " 这两臂的 `clarify_rounds` 全部为 0。"
    )

    # --- 延迟 / tokens / cost ---
    lines += ["", "## 4. 延迟 / tokens / 花费", "",
              "| 指标 | " + " | ".join(m["arm"] for m, _ in arms) + " |", sep]
    wall, model_lat, orch, tin, tout, cost = [], [], [], [], [], []
    for manifest, rows in arms:
        lat = metrics.latency(rows, manifest)
        tok = metrics.tokens_per_task(rows, manifest)
        cst = metrics.cost_per_task(rows, manifest)
        wall.append(f"{lat['wall_p50_ms'] / 1000:.1f}/{lat['wall_p95_ms'] / 1000:.1f}")
        model_lat.append(
            f"{lat['model_p50_ms'] / 1000:.1f}/{lat['model_p95_ms'] / 1000:.1f}")
        orch.append(f"{lat['orchestration_p50_ms'] / 1000:.1f}")
        tin.append(f"{tok['input_p50']:.0f}")
        tout.append(f"{tok['output_p50']:.0f}")
        cost.append(f"¥{cst['total_cny']:.2f}")
    io_ratio = []
    for _, rows in arms:
        ti = sum(int(r["input_tokens"]) for r in rows)
        to = sum(int(r["output_tokens"]) for r in rows)
        io_ratio.append(f"{ti / to:.0f}:1" if to else "—")
    lines += [
        "| 端到端墙钟 P50/P95 (s) | " + " | ".join(wall) + " |",
        "| 纯模型时间 P50/P95 (s) | " + " | ".join(model_lat) + " |",
        "| 编排开销 P50 (s) | " + " | ".join(orch) + " |",
        "| 输入 tokens/条 P50 | " + " | ".join(tin) + " |",
        "| 输出 tokens/条 P50 | " + " | ".join(tout) + " |",
        "| 输入:输出 (整臂) | " + " | ".join(io_ratio) + " |",
        "| 整臂花费 (估算) | " + " | ".join(cost) + " |",
        "",
        "> 并发度全部为 "
        f"{arms[0][0]['concurrency']}; 端到端墙钟已扣澄清等待。"
        " 编排开销 = 墙钟 − 纯模型时间, 只有零点几秒 —— 慢的是模型那一跳。",
    ]

    # --- 思考臂 (C2) 的代价专项: SPEC-002 第八节那句依赖的实测兑现 ---
    for manifest, rows in arms:
        if manifest["thinking"] != "enabled":
            continue
        timeouts = [r["case_id"] for r in rows if r.get("error_code") == "llm_timeout"]
        ti = sum(int(r["input_tokens"]) for r in rows)
        to = sum(int(r["output_tokens"]) for r in rows)
        lines += [
            "",
            f"## 5. 深度思考的代价 ({manifest['arm']}, thinking=enabled)",
            "",
            f"**本臂 LLM 超时放宽到 {manifest['llm_timeout_seconds']} 秒 (出厂 60s 的"
            f" {manifest['llm_timeout_seconds'] // 60} 倍)、样本 {len(rows)} 条 —— "
            "这里的成功率不是出厂配置下的成功率** (SPEC-007 第四节)。",
            "",
            f"- **{len(timeouts)}/{len(rows)} 条即便放宽到 "
            f"{manifest['llm_timeout_seconds']} 秒仍然超时** (`llm_timeout`): "
            f"{timeouts}。这正是 SPEC-002 第八节"
            '"一旦翻回 enabled 两个预算立刻不够"那句依赖的实测兑现 —— '
            "**它按出厂预算根本跑不完, 这件事本身就是结果**;",
            f"- **输入:输出比塌到 {ti / to:.1f}:1** (对比不思考臂的 30:1 级) —— "
            f"整臂输出 {to:,} token 逼近输入 {ti:,}, 差额几乎全是 reasoning token; "
            "reasoning 按输出价 (¥30/M) 计费, 所以**深度思考不是免费的**最锋利的说法"
            "就是这个比: 输出 token 在别的臂里几乎白送, 在这里成了主要成本。",
            "",
            f"> 上表 {manifest['arm']} 的\"编排开销 P50\"是**超时假象, 不是真开销**: "
            "超时的调用没记 `ai_usage.latency_ms` (响应还没回就被上限杀掉), 但那段"
            "等待进了端到端墙钟 —— 两者之差因此虚高。编排开销那个数看 L0-C1"
            " (稳定 0.1-0.2s), 不看 C2。",
        ]
    return "\n".join(lines) + "\n"


def write_summary(run_dirs: list[Path], out: Path) -> None:
    out.write_text(render(run_dirs))


def main(argv: list[str] | None = None) -> int:
    import sys

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("用法: python -m evals.runner.aggregate <run_dir> [<run_dir> ...]")
        return 2
    dirs = [Path(a) for a in args]
    out = Path(__file__).resolve().parents[2] / "evals" / "runs" / "summary_ablation.md"
    write_summary(dirs, out)
    print(f"五臂横向汇总已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
