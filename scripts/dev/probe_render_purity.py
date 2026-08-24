#!/usr/bin/env python3
"""探针: test_report_render 的纯度断言到底守住了什么, 又漏了什么。

做法: 把 report_render.py 真的改坏, 跑一次 pytest, 看红不红, 再原样还原。
结论 (2026-08-23 实测):

- import asyncpg          -> 红 (AssertionError, 点名 asyncpg, 不是靠退出码)
- 干净模块 import 脏模块   -> 红 (传递闭包是真的在走, 不是只看入口那一行)
- import os + os.environ  -> **全绿**。模块开头写着"不读环境变量", 而黑名单
  (asyncpg / sqlalchemy / httpx / app.db) 里没有 os —— 声明的和执行的不是一回事。
  这一条要紧, 因为 tz 显式传入的整个理由就是快照可复现;
  处置见评审意见: 入口模块改成**白名单**, 只许 import 那几个已知无副作用的标准库。

跑法: python3 scripts/dev/probe_render_purity.py   (需要 pytest, 在 apps/api 的 venv 里跑)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "apps" / "api"
RENDER = API / "app" / "services" / "report_render.py"
TEST = "tests/test_report_render.py"

MUTANTS = [
    ("import asyncpg (直接违规)",
     "import re\nfrom collections.abc import Mapping, Sequence",
     "import re\n\nimport asyncpg\nfrom collections.abc import Mapping, Sequence"),
    ("import os + os.environ (声称不做, 但没人守)",
     'MISSING_TEXT = "无此记录"',
     'import os\n\nMISSING_TEXT = os.environ.get("SENTINEL_MISSING_TEXT", "无此记录")'),
]


def run() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=API, capture_output=True, text=True,
    )
    return proc.returncode, [ln for ln in proc.stdout.splitlines() if ln.startswith("FAILED")]


def main() -> int:
    source = RENDER.read_text(encoding="utf-8")
    for name, old, new in MUTANTS:
        if source.count(old) != 1:
            print(f"锚点没命中, 跳过: {name}")
            continue
        RENDER.write_text(source.replace(old, new), encoding="utf-8")
        try:
            code, failed = run()
        finally:
            RENDER.write_text(source, encoding="utf-8")
        verdict = "红 (有人守)" if code != 0 else "**全绿 — 没人守**"
        print(f"{name:<40} 退出码={code}  {verdict}")
        for line in failed:
            print(f"    {line}")
    code, failed = run()
    print(f"\n还原后基线 退出码={code} 失败={failed or '(无)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
