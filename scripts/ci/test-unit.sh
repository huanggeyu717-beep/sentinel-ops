#!/usr/bin/env bash
# 不依赖数据库的测试: policy_engine (纯函数) + device-sim (模拟器)。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_pip_install -r requirements-dev.txt
ci_pip_install -e packages/policy_engine
ci_pip_install -e packages/scenario

# packages/scenario 也要列进来: 它有自己的测试 (IO 边界断言), 不列的话
# 以后往那个目录加的测试会静默地永远不执行 —— 和 mypy 检查目标漏加是同一类问题。
# evals 同理 (W5): 数据集 lint 与 grader 测试全部离线 (引擎 + 场景装载 + 固定
# 快照, 不连库不连网), 归单元档。
section "pytest packages/policy_engine packages/scenario apps/device-sim evals"
pytest packages/policy_engine packages/scenario apps/device-sim evals -q
