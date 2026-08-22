"""按现行渲染器重印 evals/runs/ 下全部归档的 summary.md (W5 第五批)。

测量数据是冻结的, 渲染是可以重跑的: 只读各归档的 manifest.json + results.jsonl,
只写 summary.md (archive.rerender_summary, SPEC-007 补入 33)。

唯一的特例: 20260811-003029-L2 (v1.3 开跑前探针) 的 summary.md 顶上有一段
**手写的隔离告示** (0ec0238 提交的"这一页的数字都不要引用"), 它不是渲染产物,
从 manifest + results 重生不出来。当时的手写改动是拿告示头**替换**了正文的
"# Run ..." 标题 (正文从"配置快照:"起) —— 重印后按同样结构接回: 告示头逐字
保留, 新正文去掉自己的标题行接在告示之后。

**再跑之前先确认没有别的归档被手写改过**: 上面这个特例是按 run_id 写死的,
只有它受保护。将来若给另一份归档也加了手写告示, 直接跑本脚本会把那段告示冲掉。
更稳的形状是"首行不是渲染器自己的 `# Run <id>` 标题就拒绝覆盖" —— 一个条件,
不用维护 id 清单。记在这里当债 (本脚本是一次性 dev 工具, 已完成它的用途)。

用法 (仓库根): PYTHONPATH=packages/policy_engine:packages/scenario:apps/api:. \
    .venv/bin/python scripts/dev/rerender_summaries.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.runner import archive  # noqa: E402

PROBE_RUN = "20260811-003029-L2"
PROBE_HEADER_FIRST_LINE = "# [探针] Run 20260811-003029-L2"
BODY_START = "配置快照:"  # 渲染正文标题行之后的第一行, 新旧两边都以它对齐


def main() -> int:
    runs_dir = REPO / "evals" / "runs"
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        summary_path = run_dir / "summary.md"
        hand_header: str | None = None
        if run_dir.name == PROBE_RUN:
            old = summary_path.read_text()
            if not old.startswith(PROBE_HEADER_FIRST_LINE) or old.count(BODY_START) != 1:
                print(f"!! {run_dir.name}: 手写告示头不是预期形状, 不动这一份, 先人工核对")
                return 1
            hand_header = old[: old.index(BODY_START)]
        fresh = archive.rerender_summary(run_dir)
        if hand_header is not None:
            if fresh.count(BODY_START) != 1:
                summary_path.write_text(old)  # 恢复原样, 不许留下丢了告示头的中间态
                print(f"!! {run_dir.name}: 重印正文里找不到唯一的'{BODY_START}', 先人工核对")
                return 1
            summary_path.write_text(hand_header + fresh[fresh.index(BODY_START):])
        print(f"重印 {run_dir.name}/summary.md" + (" (告示头已接回)" if hand_header else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
