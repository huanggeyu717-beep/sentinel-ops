#!/usr/bin/env bash
# 不依赖数据库的测试: policy_engine (纯函数) + device-sim (模拟器)。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_pip_install -r requirements-dev.txt
ci_pip_install -e packages/policy_engine
ci_pip_install -e packages/scenario

# packages/scenario 也要列进来: 它有自己的测试 (IO 边界断言), 不列的话
# 以后往那个目录加的测试会静默地永远不执行 —— 和 mypy 检查目标漏加是同一类问题。
#
# evals 只收 evals/tests/ 这一个目录 (W5 修): 这个 job 装的是 requirements-dev.txt
# (ruff / mypy / pytest / pydantic / PyYAML) 加两个 -e 包 —— 没有 httpx 也没有
# asyncpg。原来写的是整个 evals, 而 evals/runner/ 下有 import httpx / asyncpg 的
# 模块, 于是收集期就挂。需要那两个依赖的测试已挪去 evals/runner/tests/, 由 api job
# 跑 (那个 job 本来就装了 apps/api 全套依赖)。
#
# 分界线是**装得上什么**, 不是**测的是什么**: runner 里 metrics / aggregate 是
# 纯函数, 它们的测试留在 evals/tests/。按实际 import 判, 不按文件名猜。
# 这条约定由 evals/tests/test_grader_io_boundary.py 的传递 import 断言守着,
# 不靠人记得 —— 光靠"下次注意"守不住, 本项目在这上面栽过三次。
section "pytest packages/policy_engine packages/scenario apps/device-sim evals/tests"
pytest packages/policy_engine packages/scenario apps/device-sim evals/tests -q
