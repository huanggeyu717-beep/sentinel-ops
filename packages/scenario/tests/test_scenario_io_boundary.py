"""把 __init__.py 里那条 IO 边界从"注释"变成"CI 能拦住的规则"。

只写在 docstring 里的约定拦不住任何人: 半年后有人图方便往这里塞一个 HTTP 客户端,
包就退化成第二个模拟器 —— 那正是 SPEC-005 选方案 B 要避免的。
ruff 只会在"导入了但没用"时报错, 真用起来它不管; mypy 更不管。所以这里自己查。

本包的边界: **允许读场景文件, 禁止网络与数据库**。
(policy_engine 的边界更严, 见那边同名测试。)
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "scenario"

BANNED_PREFIXES = {
    # 网络
    "urllib", "http", "socket", "requests", "httpx", "aiohttp", "websockets",
    # 数据库
    "asyncpg", "sqlalchemy", "psycopg", "psycopg2", "sqlite3", "redis",
    # 起子进程绕过上面两条也不行
    "subprocess",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return {name.split(".")[0] for name in names}


def test_io_boundary__no_network_or_database_imports():
    offenders: list[str] = []
    for source in sorted(PACKAGE_DIR.rglob("*.py")):
        for module in _imported_modules(source) & BANNED_PREFIXES:
            offenders.append(f"{source.relative_to(PACKAGE_DIR.parent)} 导入了 {module}")
    assert not offenders, (
        "scenario 包只负责读场景与换算时间轴, 不该碰网络或数据库。\n"
        "HTTP 投递属于调用方 (sim.py 或 API 的 drill 服务):\n  " + "\n  ".join(offenders)
    )
