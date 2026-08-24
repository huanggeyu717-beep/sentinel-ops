#!/usr/bin/env python3
"""变异架子: conftest.preserve_users 的复原逻辑, 由评审方另挑靶子独立验证。

实现者自己验过"整段复原回退掉 -> 红"。本脚本换三个**它没试过**的方向, 重点是
第二条: **只跳过序列还原**。复原了行、没复原序列, 从"id 有没有变"这个角度看
一切正常, 而序列被留在高位 —— 下一个走序列插的用户会拿到一个远超预期的号,
正是本次事故 (viewer=102 而 bo=104) 那副牌面的来源。
守不住它, 这个修复就只修了看得见的那一半。

跑法 (需要本地 Postgres, 先 source .venv/bin/activate):
    python3 scripts/dev/mutate_preserve_users.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1].parent
API = ROOT / "apps" / "api"
CONFTEST = API / "tests" / "conftest.py"
TESTS = ["apps/api/tests/test_policy_constraints.py"]

_VENV = ROOT / ".venv" / "bin" / "python"
PYTHON = str(_VENV) if _VENV.exists() else sys.executable

_SETVAL = '''        await conn.execute(
            "SELECT setval(pg_get_serial_sequence('users','id'), $1, $2)",
            seq["last_value"], seq["is_called"],
        )'''

MUTANTS = [
    ("P1 复原整段变成空操作 (实现者已验过, 作对照)",
     "    async def restore(conn, snap) -> None:\n"
     "        users, roles, seq = snap",
     "    async def restore(conn, snap) -> None:\n"
     "        return\n"
     "        users, roles, seq = snap"),

    ("P2 **只跳过序列还原** (行复原了, 序列留在高位)",
     _SETVAL,
     "        pass  # 变异: 不还原序列"),

    ("P3 不复原 user_roles (人回来了, 角色没回来)",
     '            for r in roles:\n                if r["user_id"] == u["id"]:',
     '            for r in ():\n                if r["user_id"] == u["id"]:'),
]


def run() -> tuple[int, list[str], str]:
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True,
    )
    failed = sorted({
        line.split("::", 1)[1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED") and "::" in line
    })
    return proc.returncode, failed, proc.stdout + proc.stderr


def main() -> int:
    print(f"解释器: {PYTHON}")
    code, failed, output = run()
    if code != 0:
        print(f"基线就不是绿的, 先修基线。退出码={code} 失败={failed}")
        print(output.strip()[-2000:])
        print("退出码含义: 1=有测试失败 2=被中断 3=内部错误 4=用法错误 5=一条都没收集到")
        return 1
    print("基线绿。开始变异。\n")
    print(f"{'变异':<46}{'退出码':<8}红掉的测试")
    print("-" * 110)
    unguarded = []
    for name, old, new in MUTANTS:
        source = CONFTEST.read_text(encoding="utf-8")
        if source.count(old) != 1:
            print(f"{name:<46}锚点命中 {source.count(old)} 次, 跳过 (代码已变, 请更新本脚本)")
            continue
        CONFTEST.write_text(source.replace(old, new), encoding="utf-8")
        try:
            code, failed, _ = run()
        finally:
            CONFTEST.write_text(source, encoding="utf-8")
        tail = "   <<<< 全绿, 这条没人守" if code == 0 else ""
        if code == 0:
            unguarded.append(name)
        print(f"{name:<46}{code:<8}{', '.join(failed) if failed else '(无)'}{tail}")
    code, failed, _ = run()
    print("-" * 110)
    print(f"还原后基线 退出码={code} 失败={failed or '(无)'}")
    if unguarded:
        print("\n没人守的变异:")
        for name in unguarded:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
