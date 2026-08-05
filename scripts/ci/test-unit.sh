#!/usr/bin/env bash
# 不依赖数据库的测试: policy_engine (纯函数) + device-sim (模拟器)。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_pip_install -r requirements-dev.txt
ci_pip_install -e packages/policy_engine

section "pytest packages/policy_engine apps/device-sim"
pytest packages/policy_engine apps/device-sim -q
