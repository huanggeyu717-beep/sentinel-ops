#!/usr/bin/env python3
"""变异 M5 的两半 (SPEC-007 十一节): 谁在守 `AblationProfile`, 各守什么。

M5 原文只写"把某一档开关写死成 production 的值", 于是第一轮只做了一半:

- **M5a** (已做): 把 production 为 True 的开关**写死成 True** ——
  非 production 档跟着被打开, 守它的是**分档金样** (A0/A1 的步骤序列);
  验收 14 (字段表) 与验收 15 (production 与默认路径同序列) 都照样绿;
- **M5b** (本次补): 把 production 为 True 的开关**写死成 False** ——
  守它的应该是验收 15。**两者守的不是一回事**: 15 守"production 档没被改坏",
  分档金样守"非 production 档没被改坏"。只做 M5a 等于只有后半边有人守。

M5b 有三个可下手的位置, 结论不同 —— 一刀切地问"15 红不红"会得到错误的安心感,
所以本脚本三个都跑, 把矩阵摆出来:

- `production_body`   改 `AblationProfile.production()` 的返回值 (两条路径同时变);
- `resolve_default`   只改 `_resolve_profile(None)` 那条默认路径 (两条路径分家);
- `call_site`         profile 字段不动, 只在**消费点**把能力关掉。

用法: .venv/bin/python scripts/dev/mutation_m5.py [变异名 ...]
不传参数跑全部。脚本改完源码必定还原 (finally), 但它确实会临时改文件 ——
别在有未保存改动的工作树上跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "apps/api/app/services/agent_runtime.py"

# 变异名 -> (文件, 原文, 改后)
MUTANTS: dict[str, tuple[Path, str, str]] = {
    # production() 的返回值本身: 显式 production() 与默认路径同时变
    "production_body": (
        RUNTIME,
        "            inventory_in_prompt=False,\n"
        "            discovery_tools=True,\n"
        "            validate_and_repair=True,\n"
        "            clarification=True,\n",
        "            inventory_in_prompt=False,\n"
        "            discovery_tools=True,\n"
        "            validate_and_repair=True,\n"
        "            clarification=False,  # M5b 变异\n",
    ),
    # 只把默认路径 (不传 profile) 改坏 —— 两条路径分家, 正是验收 15 的靶心
    "resolve_default": (
        RUNTIME,
        "    return AblationProfile.from_level(settings().agent_ablation_level)",
        "    _m = AblationProfile.from_level(settings().agent_ablation_level)\n"
        "    return replace(_m, clarification=False)  # M5b 变异",
    ),
    # profile 字段全对, 只在消费点把能力关掉 (工具清单裁剪那一处)
    "call_site": (
        RUNTIME,
        "    if not st.caps.clarification:\n"
        "        names = tuple(n for n in names if n != \"ask_clarification\")",
        "    if True:  # M5b 变异 (原: if not st.caps.clarification)\n"
        "        names = tuple(n for n in names if n != \"ask_clarification\")",
    ),
}

# 三层守卫各自的测试选择器 (与 test_agent_ablation.py 头部注释同名)
GUARDS: dict[str, list[str]] = {
    "验收 14 字段表": [
        "test_profile_production__matches_runtime_default_fieldwise",
        "test_profile_from_level__each_level_fieldwise",
    ],
    "验收 15 步骤序列": ["test_profile_production__same_steps_as_no_profile"],
    "分档金样 A0/A1": ["test_ablation_a0__", "test_ablation_a1__"],
}


def run_guard(selectors: list[str]) -> bool:
    """True = 绿 (没抓住), False = 红 (抓住了)。"""
    expr = " or ".join(selectors)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "apps/api/tests/test_agent_ablation.py",
         "-q", "--no-header", "-k", expr, "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
    )
    return result.returncode == 0


def apply_and_measure(name: str) -> dict[str, bool]:
    path, old, new = MUTANTS[name]
    original = path.read_text()
    if old not in original:
        raise SystemExit(f"变异 {name} 的原文没匹配上 —— 源码变了, 先更新本脚本")
    patched = original.replace(old, new, 1)
    if name == "resolve_default":
        patched = patched.replace(
            "from dataclasses import dataclass",
            "from dataclasses import dataclass, replace", 1,
        )
    try:
        path.write_text(patched)
        return {guard: run_guard(sels) for guard, sels in GUARDS.items()}
    finally:
        path.write_text(original)


def run_broad(name: str) -> tuple[int, str]:
    """三层守卫之外, 整个 Agent 测试面还有谁会红 —— `--broad <变异名>`。

    存在的理由: `call_site` 那一格三层守卫全绿, 于是"到底还有没有人守"这个问题
    不能靠推断回答。答案要么是一串具体的红测试名, 要么是"确实没人守"。
    """
    path, old, new = MUTANTS[name]
    original = path.read_text()
    patched = original.replace(old, new, 1)
    try:
        path.write_text(patched)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "apps/api/tests", "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True,
        )
        failed = [
            line for line in result.stdout.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        ]
        return result.returncode, "\n".join(failed) or result.stdout[-1500:]
    finally:
        path.write_text(original)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--broad":
        name = argv[1]
        print(f"== M5b / {name}: 整个 apps/api/tests 谁会红 ==")
        code, detail = run_broad(name)
        print(f"退出码 {code} ({'全绿 —— 没有任何测试守着它' if code == 0 else '有红'})")
        print(detail)
        return 0

    names = argv or list(MUTANTS)
    print("== 基线 (无变异): 三层守卫应全绿 ==")
    baseline = {guard: run_guard(sels) for guard, sels in GUARDS.items()}
    for guard, green in baseline.items():
        print(f"  {guard}: {'绿' if green else '红 !!'}")
    if not all(baseline.values()):
        print("基线就不绿, 变异结论无意义 —— 先修基线")
        return 1

    rows: dict[str, dict[str, bool]] = {}
    for name in names:
        print(f"\n== M5b / {name} ==")
        rows[name] = apply_and_measure(name)
        for guard, green in rows[name].items():
            print(f"  {guard}: {'绿 (没抓住)' if green else '**红 (抓住了)**'}")

    print("\n== 矩阵 (红 = 抓住了) ==")
    guards = list(GUARDS)
    print("| 变异 | " + " | ".join(guards) + " |")
    print("|---|" + "---|" * len(guards))
    for name, result in rows.items():
        print(f"| {name} | "
              + " | ".join("绿" if result[g] else "**红**" for g in guards) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
