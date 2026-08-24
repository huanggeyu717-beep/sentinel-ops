#!/usr/bin/env python3
"""变异架子: SPEC-008 第二段那几条关键不变量, 由评审方另写锚点独立验证。

做法与第一段那两个探针一致 —— **把实现真改坏, 真跑一次 pytest, 看红不红,
再原样还原**。"我觉得这里有测试守着"不算数, 本项目为此栽过九次。

六条里有一条 (B1b) 是**反方向**的: 它检查"clarifying 不结算"这条 SPEC-009 的
不变量有没有人守。只测"该回补的时候回补了"、不测"不该回补的时候没回补",
把回补集合写成"全都回补"照样全绿 —— 与第一段那条"计数器恒返回 1"同一课。

跑法 (需要本地 Postgres, 与 test-api.sh 同一个库):
    python3 scripts/dev/mutate_spec008_stage2.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1].parent
API = ROOT / "apps" / "api"
BUDGET = API / "app" / "services" / "budget_service.py"
TASKSVC = API / "app" / "services" / "report_task_service.py"

TESTS = [
    "apps/api/tests/test_report_task_service.py",
    "apps/api/tests/test_report_runtime.py",
    "apps/api/tests/test_reports_http.py",
    "apps/api/tests/test_budget_settlement.py",
]

_REFUND_OLD = '''_REFUNDABLE_OUTCOMES = frozenset(
    {"awaiting_approval", "awaiting_review", "failed", "dead_letter"}
)'''

MUTANTS = [
    ("B1  回补集合去掉 awaiting_review (每份报告漏一笔预扣)", BUDGET, _REFUND_OLD,
     '_REFUNDABLE_OUTCOMES = frozenset({"awaiting_approval", "failed", "dead_letter"})'),

    ("B1b 回补集合加上 clarifying (反方向: 不该回补的回补了)", BUDGET, _REFUND_OLD,
     '_REFUNDABLE_OUTCOMES = frozenset(\n'
     '    {"awaiting_approval", "awaiting_review", "clarifying", "failed", "dead_letter"}\n'
     ')'),

    ("B2  建任务时不显式写 stage=collecting (会掉进策略状态机)", TASKSVC,
     '        await session.execute(_SET_COLLECTING, {"id": created["task_id"]})',
     '        pass  # 变异: 不写 collecting'),

    ("B3  拿掉 service 那一次跨用户去重查询", TASKSVC,
     "    if existing is not None:",
     "    if False and existing is not None:"),

    ("B4  人退回之后不再把任务收进 completed", TASKSVC,
     '    if row["status"] == "draft":\n'
     '        await _complete_review_task(session, row["task_id"], "returned")',
     '    if False:\n'
     '        await _complete_review_task(session, row["task_id"], "returned")'),

    ("B5  渲染改成重算事实包而不是读快照 (SPEC 变异 11.3)", TASKSVC,
     "    result = check_draft(body, _facts_from_snapshot(fact_pack))",
     "    result = check_draft(\n"
     "        body,\n"
     "        _facts_from_snapshot(await load_fact_pack(session, row['incident_id'])),\n"
     "    )"),
]


# 用仓库的 venv 解释器, 不用跑本脚本的那个 —— 系统 python3 里没有 pytest 与
# asyncpg, 拿它跑会得到一个和代码无关的错误。
_VENV = ROOT / ".venv" / "bin" / "python"
PYTHON = str(_VENV) if _VENV.exists() else sys.executable


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
    return proc.returncode, failed, (proc.stdout + proc.stderr)


def main() -> int:
    print(f"解释器: {PYTHON}")
    code, failed, output = run()
    if code != 0:
        # 把真实输出印出来 —— 只报退出码等于什么都没说 (SPEC-007 补入 40 同一课,
        # 这个脚本第一版就犯了: 退出码 4 是 pytest 的"用法错误", 与测试红不红无关)
        print(f"基线就不是绿的, 先修基线再跑变异。退出码={code} 失败={failed}")
        print("-" * 72)
        print(output.strip()[-3000:])
        print("-" * 72)
        print("退出码含义: 1=有测试失败 2=被中断 3=内部错误 4=用法错误 "
              "(通常是路径不存在或解释器没装 pytest) 5=一条都没收集到")
        return 1
    print("基线绿。开始变异。\n")
    print(f"{'变异':<52}{'退出码':<8}红掉的测试")
    print("-" * 120)
    unguarded = []
    for name, path, old, new in MUTANTS:
        source = path.read_text(encoding="utf-8")
        if source.count(old) != 1:
            print(f"{name:<52}锚点命中 {source.count(old)} 次, 跳过 (代码已变, 请更新本脚本)")
            continue
        path.write_text(source.replace(old, new), encoding="utf-8")
        try:
            code, failed, _ = run()
        finally:
            path.write_text(source, encoding="utf-8")
        if code == 0:
            unguarded.append(name)
            tail = "   <<<< 全绿, 这条没人守"
        else:
            tail = ""
        print(f"{name:<52}{code:<8}{', '.join(failed) if failed else '(无)'}{tail}")
    code, failed, _ = run()
    print("-" * 120)
    print(f"还原后基线 退出码={code} 失败={failed or '(无)'}")
    if unguarded:
        print("\n没人守的变异:")
        for name in unguarded:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
