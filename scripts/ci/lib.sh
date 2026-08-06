#!/usr/bin/env bash
# scripts/ci/ 下所有脚本共用的小工具。不单独执行, 由其它脚本 source。
#
# 设计意图: CI 和本机跑的是同一份脚本文件, 差别只有"要不要装依赖"这一件事。
# GitHub Actions 会自动设置环境变量 CI=true, 本机没有 —— 用它区分。

set -euo pipefail

# 无论从哪个目录调用, 都切到仓库根目录再干活
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 只在 CI 上装依赖; 本机跑时不碰你的 Python 环境
ci_pip_install() {
  if [ "${CI:-}" = "true" ]; then
    python -m pip install --quiet --disable-pip-version-check "$@"
  fi
}

section() {
  echo ""
  echo "==> $*"
}
