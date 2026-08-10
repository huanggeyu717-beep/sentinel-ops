#!/usr/bin/env bash
# 静态检查。CI 与本机是同一个文件 —— 这是"本机绿 = CI 绿"的结构性保证。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGETS=(packages apps/api apps/device-sim evals)

ci_pip_install -r requirements-dev.txt

section "ruff check ${TARGETS[*]}"
if [ "${CI:-}" = "true" ]; then
  # --output-format=github: 报错直接标注到 PR 的对应代码行上
  ruff check --output-format=github "${TARGETS[@]}"
else
  bash scripts/ci/check-tool-versions.sh
  ruff check "${TARGETS[@]}"
fi

# mypy 以前只在本机 make lint 里跑, CI 完全不跑 —— 等于它拦不住任何东西。
# 放进这里之后, ruff 和 mypy 两道检查在本机与 CI 执行的是同一份脚本。
# evals 是 W5 加的: 不列进目标的话, mypy.ini 里 evals.graders.* 的严格档白名单
# 是空转的 —— 白名单只对被扫描的文件生效 (与"检查目标漏加"同一类问题)。
section "mypy apps/api packages/policy_engine packages/scenario evals"
mypy apps/api packages/policy_engine packages/scenario evals
