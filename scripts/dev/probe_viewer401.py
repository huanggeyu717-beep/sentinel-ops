#!/usr/bin/env python3
"""一条命令复现 viewer 401 并测出机制: DROP sentinel_test -> 全量跑 apps/api/tests
(挂上 probe_viewer401_plugin 旁观) -> 打印探针日志。

要回答的问题: **同一次失败的跑里, viewer 的 id 变过没有? 变化发生在哪条用例?**

用法 (从仓库根, 进了 venv 再跑):
    python scripts/dev/probe_viewer401.py

输出自足: 最后整段打印探针日志 (token 签发记录 / viewer id 变化点 /
get_user 返回 None 的现场 / 收尾的 users 全表与序列状态)。
探针只旁观不干预 —— 它不改断言、不动测试顺序, 失败照常失败。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = REPO_ROOT / "scripts" / "dev" / "probe_viewer401.log"

TEST_URL = os.environ.get(
    "SENTINEL_TEST_DATABASE_URL",
    "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel_test",
)
DSN = TEST_URL.replace("+asyncpg", "")


def drop_test_database() -> None:
    import asyncpg

    async def go() -> None:
        admin_dsn = DSN.rsplit("/", 1)[0] + "/postgres"
        db_name = DSN.rsplit("/", 1)[1]
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            print(f"已 DROP {db_name} (全新库复现条件)")
        finally:
            await conn.close()

    asyncio.run(go())


def main() -> None:
    LOG_PATH.unlink(missing_ok=True)
    drop_test_database()

    env = dict(os.environ)
    env["PROBE_VIEWER401_LOG"] = str(LOG_PATH)
    # -p 的插件在 pytest.ini 的 pythonpath 生效之前就要 import, 走 PYTHONPATH
    extra = str(REPO_ROOT / "scripts" / "dev")
    env["PYTHONPATH"] = extra + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "apps/api/tests",
         "-p", "probe_viewer401_plugin", "-q"],
        cwd=REPO_ROOT, env=env,
    )

    print("\n================ 探针日志 ================")
    if LOG_PATH.exists():
        print(LOG_PATH.read_text(encoding="utf-8"), end="")
    else:
        print("(探针没写出日志 —— 插件没被装载?)")
    print("==========================================")
    print(f"pytest 退出码: {result.returncode}")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
