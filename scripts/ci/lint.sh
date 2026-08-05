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
