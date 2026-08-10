"""两条 import 边界断言, 同一个手法 (AST 扫 import), 守的是两件不同的事。

**一、grader 零模型调用** (SPEC-007 验收 13): `evals/graders/` 下不许出现网络 /
模型 / 数据库 / 进程边界的 import。判分必须零方差、零调用成本; grader 里混进一个
httpx import 不会有任何报错, 但"确定性"三个字就没了。

**二、`evals/tests/` 真的离线** (W5 CI 修复): 这个目录跑在 CI 的 engine job 里,
那个 job 只装 requirements-dev.txt (ruff / mypy / pytest / pydantic / PyYAML) 加
`policy_engine` / `scenario` 两个 -e 包。往里放一个装不到的依赖, 整个 job 在
**收集期**就挂 —— 而且本机是绿的, 因为本机 venv 有 apps/api 全套依赖。
这正是 W5 那次 CI 红的形状, 也是本项目第三次"本机绿、CI 红"。

第二条为什么必须**传递地**扫, 不能只看测试文件自己的第一层 import:
`test_runner_extract.py` 整个文件里根本没有 asyncpg 三个字, 它只写了
`from evals.runner.extract import build_outcome` —— asyncpg 在 extract.py 里。
只扫第一层的话, 这条断言会对着当初真正让 CI 红掉的那个文件说"没问题"。

第二条用的是**白名单**而不是黑名单: 第三方根必须在 `ALLOWED_THIRD_PARTY` 里,
它就是 requirements-dev.txt 加两个 -e 包的投影。黑名单只挡得住想得到的那几个,
白名单挡得住下一个还没出现的 —— 新依赖必须有人显式加一行, 而加那一行的时候
自然会看见上面这段说明。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GRADERS = REPO / "evals" / "graders"
OFFLINE_TESTS = REPO / "evals" / "tests"

# ===== 一、grader 的黑名单 =====

# 模型/网络/进程边界类模块, grader 一个都不许碰
FORBIDDEN_PREFIXES = (
    "httpx", "requests", "urllib", "socket", "aiohttp", "openai", "anthropic",
    "subprocess", "asyncio",
    "app.",  # apps/api 的任何模块 (llm_client 在里面; grader 只吃结构化输入)
    "sqlalchemy", "asyncpg", "psycopg",
)

# ===== 二、离线测试档的白名单 =====

# engine job 里装得到的第三方顶层包 = requirements-dev.txt + 两个 -e 包。
# 往这里加名字之前先问: 那个包会不会被装进 engine job? 不会就别加, 把测试挪去
# evals/runner/tests/ (api 档)。
ALLOWED_THIRD_PARTY = frozenset({
    "pytest",         # requirements-dev.txt
    "pydantic",       # requirements-dev.txt
    "yaml",           # requirements-dev.txt (PyYAML)
    "policy_engine",  # pip install -e packages/policy_engine
    "scenario",       # pip install -e packages/scenario
})

# 会被**走进去继续扫**的第一方根 -> 它的源码根目录。
# `app` 在列, 是因为 test_dataset_lint 要从 app.services.agent_slots 读
# MISSING_SLOTS 枚举 (SPEC-007: 槽位不许抄第三份, 必须与工具 Schema 同源)。
# 那个模块的 docstring 承诺自己零依赖 —— 走进去扫, 这条承诺就有人守了;
# 哪天有人往它里面加一个 sqlalchemy import, 这条断言先红, 而不是 CI 收集期挂掉。
FIRST_PARTY_ROOTS = {
    "evals": REPO,
    "app": REPO / "apps" / "api",
}


def _imports(path: Path) -> set[str]:
    """文件里出现的 import 目标 (只含绝对导入), 用于第一条黑名单断言。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _import_targets(path: Path, dotted: str) -> set[str]:
    """文件里出现的 import 目标, **相对导入已解析成绝对模块名**。

    `dotted` 是这个文件自己的模块名。相对导入必须解析, 否则
    `from . import metrics` / `from .reference_runner import ...` 这类边一条都走不到,
    传递闭包会在第一个包内部导入那里断掉 —— 断得静悄悄, 看起来还是绿的。
    """
    package = dotted if path.name == "__init__.py" else dotted.rpartition(".")[0]
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")
                keep = len(parts) - node.level + 1
                head = package if node.level == 1 else ".".join(parts[:keep])
                base = f"{head}.{node.module}" if node.module else head
            if not base:
                continue
            targets.add(base)
            # `from evals.runner import aggregate` 里, 被 import 的名字**本身就是个模块**。
            # 只收 base 的话这条边走不到 aggregate.py —— 而 `from X import y` 正是本仓库
            # 最常见的写法。多出来的那些"其实是函数名"的候选解析不到文件, 自动被丢掉。
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _resolve(dotted: str) -> Path | None:
    """第一方模块名 -> 源文件; 不是第一方 (或找不到) 返回 None。"""
    root, _, _ = dotted.partition(".")
    base = FIRST_PARTY_ROOTS.get(root)
    if base is None:
        return None
    candidate = base.joinpath(*dotted.split("."))
    if (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    return None


def _reachable(entry_points: dict[str, Path]) -> dict[str, Path]:
    """从入口文件出发, 沿第一方 import 求传递闭包。"""
    seen = dict(entry_points)
    queue = list(entry_points.items())
    while queue:
        dotted, path = queue.pop()
        for target in _import_targets(path, dotted):
            resolved = _resolve(target)
            if resolved is None or target in seen:
                continue
            seen[target] = resolved
            queue.append((target, resolved))
    return seen


def test_graders__no_model_or_network_imports():
    files = sorted(GRADERS.glob("*.py"))
    assert files, "graders 目录不该是空的"
    for f in files:
        for name in _imports(f):
            assert not name.startswith(FORBIDDEN_PREFIXES), (
                f"{f.name} import 了 {name} —— grader 必须零模型调用、零网络、"
                f"零数据库 (SPEC-007 验收 13)"
            )


def test_offline_tests__only_import_what_the_unit_job_installs():
    # 入口收全目录的 *.py 而不是 test_*.py: __init__.py 在收集期也会被 import,
    # 它里面一个坏 import 同样能把整个 job 干掉。evals/__init__.py 同理 (父包)。
    entry_points = {
        f"evals.tests.{f.stem}" if f.name != "__init__.py" else "evals.tests": f
        for f in sorted(OFFLINE_TESTS.glob("*.py"))
    }
    entry_points["evals"] = REPO / "evals" / "__init__.py"
    assert any(k.startswith("evals.tests.test_") for k in entry_points), (
        "evals/tests 下不该一条测试都没有"
    )

    reachable = _reachable(entry_points)
    # 传递闭包必须真的走出去过 —— 否则解析逻辑坏掉时这条断言会对着空集喊通过,
    # 又是一次"空集上的全称命题恒真" (W5 已经栽过三次的形状)。
    assert any(m.startswith("evals.graders.") for m in reachable), (
        "传递闭包没走到 evals.graders.* —— 相对导入解析多半坏了, "
        "这条断言正在检查一个空集"
    )

    for dotted, path in sorted(reachable.items()):
        for target in sorted(_import_targets(path, dotted)):
            root, _, _ = target.partition(".")
            if root in FIRST_PARTY_ROOTS or root in ALLOWED_THIRD_PARTY:
                continue
            assert root in sys.stdlib_module_names, (
                f"{path.relative_to(REPO)} (由 evals/tests 传递 import 到) "
                f"import 了 {target} —— CI 的 engine job 装不到它, "
                f"整个 job 会在收集期挂掉。要么把测试挪去 evals/runner/tests/ (api 档), "
                f"要么把 {root} 加进 ALLOWED_THIRD_PARTY 并确认它真的进了 "
                f"requirements-dev.txt"
            )
