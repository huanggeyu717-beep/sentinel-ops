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

# 本机前置检查: 不在 CI 且关键模块 import 不到时, 打印一句人话再退出。
# 不加这道闸, 没进 venv 直接跑会得到 "ModuleNotFoundError: No module named
# 'asyncpg'" —— 看起来像代码坏了, 其实是环境没配好, 与 pytest.ini 顶部注释
# 警告过的坑同形, 只是换了个模块名。CI 上跳过 (依赖由 ci_pip_install 现装)。
require_modules_outside_ci() {
  if [ "${CI:-}" = "true" ]; then return 0; fi
  local mod
  for mod in "$@"; do
    if ! python -c "import ${mod}" >/dev/null 2>&1; then
      echo "本机跑请先 source .venv/bin/activate (当前 python import 不到 ${mod})" >&2
      exit 1
    fi
  done
}

# ===== 静态检查目标: 单一事实源 =====
#
# ruff、mypy、make lint-fix 以前各写各的清单。`make lint` 调的是 lint.sh 所以与 CI
# 同源, 但 `make lint-fix` 自己抄了一份 —— 那是本机与 CI 之间**唯一会漂的地方**,
# 而 ADR-005 整篇讲的就是"本机与 CI 执行同一份东西"。三处现在都读这个变量。
#
# 按**目录**收, 不逐个点名包 (W5 收尾改): mypy 与 test-unit.sh 原来写的是
# `packages/policy_engine packages/scenario`, 而 ruff 写的是 `packages` ——
# 出现第三个包时 ruff 收得到、另外两个静默漏掉, 同一个仓库两套口径。
# 这与 test-unit.sh 那句"不列的话以后往那个目录加的测试会永远不执行"是同一个坑。
#
# scripts/dev 是 W5 收尾加的: 之前它一道静态检查都没有, 而
# record_cassettes.py / probe_*.py 是**仓库里唯一会真花钱的代码**,
# CLAUDE.md 协作红线还专门要求临时脚本必须落在那个目录。最该被看一眼的地方
# 反而没人看。加进来时零改动即通过 (ruff 干净, mypy 默认档干净)。
#
# 注意 scripts/dev 走**默认档**, 不进 mypy.ini 的严格档白名单: 那些是探针,
# 强制标注收益低、噪音大 (ADR-005 对测试代码的同一条理由)。
# scripts/ops 是 W6 加的 (每日重置脚本): 它是一段会删数据的代码, 和 scripts/dev
# 的花钱脚本同理 —— 最该被看一眼的地方不能没人看。走 mypy 默认档 (同 scripts/dev)。
LINT_TARGETS=(packages apps/api apps/device-sim evals scripts/dev scripts/ops)
