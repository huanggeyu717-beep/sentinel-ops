"""grader 零模型调用 (SPEC-007 验收 13): AST 扫描 evals/graders 全部模块的
import, 网络/模型客户端一律不许出现 —— 手法与两个包的 IO 边界断言一致
(本项目第八处"把约定变成 CI 能拦的规则")。

判分必须零方差、零调用成本; grader 里混进一个 httpx import 不会有任何报错,
但"确定性"三个字就没了。
"""
from __future__ import annotations

import ast
from pathlib import Path

GRADERS = Path(__file__).resolve().parents[1] / "graders"

# 模型/网络/进程边界类模块, grader 一个都不许碰
FORBIDDEN_PREFIXES = (
    "httpx", "requests", "urllib", "socket", "aiohttp", "openai", "anthropic",
    "subprocess", "asyncio",
    "app.",  # apps/api 的任何模块 (llm_client 在里面; grader 只吃结构化输入)
    "sqlalchemy", "asyncpg", "psycopg",
)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_graders__no_model_or_network_imports():
    files = sorted(GRADERS.glob("*.py"))
    assert files, "graders 目录不该是空的"
    for f in files:
        for name in _imports(f):
            assert not name.startswith(FORBIDDEN_PREFIXES), (
                f"{f.name} import 了 {name} —— grader 必须零模型调用、零网络、"
                f"零数据库 (SPEC-007 验收 13)"
            )
