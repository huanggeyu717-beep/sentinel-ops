#!/usr/bin/env python3
"""给已有 run 归档回填 artifact 字段 (一次性, L0 停顿点决定 2)。

run 20260810-130256-L0 跑在 artifact 字段落地之前; 评测库在下一臂开跑时会被
重置, 那 4 条注入得逞草案与 "9 区被猜成存在的区" 的原文只活在库里 —— 趁还在,
从库里按 task_id 重建 artifact 写回 results.jsonl, manifest 记一笔回填时间。

用法: .venv/bin/python scripts/dev/backfill_run_artifact.py evals/runs/<run_id>
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in ("", "apps/api", "packages/policy_engine", "packages/scenario"):
    sys.path.insert(0, str(REPO / p))

import asyncpg  # noqa: E402

from evals.runner import extract  # noqa: E402
from evals.runner.arms import app_settings  # noqa: E402


async def main(run_dir: Path) -> None:
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    if all("artifact" in r for r in rows):
        print("全部行已有 artifact, 无事可做")
        return
    conn = await asyncpg.connect(extract.plain_dsn(str(app_settings().eval_database_url)))
    try:
        for row in rows:
            record = await extract.fetch_task_record(conn, int(row["task_id"]))
            outcome = extract.build_outcome(record)
            row["artifact"] = {
                "final_policy": outcome.final_draft_body,
                "draft_bodies": list(outcome.all_draft_bodies),
                "missing_slots": [list(r) for r in outcome.clarify_slot_rounds],
                "error_codes": list(outcome.validation_codes),
                "tool_calls": list(outcome.executed_tools),
                "terminal_status": {
                    "status": outcome.final_status,
                    "error_code": outcome.error_code,
                    "draft_version_status": outcome.draft_version_status,
                },
            }
    finally:
        await conn.close()
    with (run_dir / "results.jsonl").open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["artifact_backfilled_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"已回填 {len(rows)} 行 artifact 并在 manifest 记录回填时间: {run_dir}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
