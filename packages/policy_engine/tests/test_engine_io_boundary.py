"""把 CLAUDE.md 不变量 2 的"零 IO"变成 CI 能拦住的规则。

不变量 2 说 packages/policy_engine 零 IO: 纯函数吃事件流。
这条是"执行器与模拟器共用同一份 evaluate()"的前提 —— 一旦引擎里出现读文件或连库,
线上执行与模拟回放就不可能真正等价, 而那是整个项目最核心的一条主张。

比 scenario 包更严: 连读文件都不允许 (含内置的 open())。
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "policy_engine"

BANNED_PREFIXES = {
    "urllib", "http", "socket", "requests", "httpx", "aiohttp", "websockets",
    "asyncpg", "sqlalchemy", "psycopg", "psycopg2", "sqlite3", "redis",
    "subprocess", "pathlib", "os", "io", "shutil", "tempfile", "csv",
}


def _imports_and_calls(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    return {m.split(".")[0] for m in modules}, calls


def test_zero_io__no_file_network_or_database_access():
    offenders: list[str] = []
    for source in sorted(PACKAGE_DIR.rglob("*.py")):
        modules, calls = _imports_and_calls(source)
        rel = source.relative_to(PACKAGE_DIR.parent)
        offenders += [f"{rel} 导入了 {m}" for m in sorted(modules & BANNED_PREFIXES)]
        if "open" in calls:
            offenders.append(f"{rel} 调用了内置 open()")
    assert not offenders, (
        "policy_engine 必须零 IO (CLAUDE.md 不变量 2): 纯函数吃事件流,\n"
        "这样线上执行器与模拟器才可能共用同一份 evaluate()。\n  " + "\n  ".join(offenders)
    )
