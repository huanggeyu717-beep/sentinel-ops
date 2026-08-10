#!/usr/bin/env bash
# 在一个**全新的空 venv** 里复现某一个 CI job。
#
# 为什么需要这个东西:
#   本项目已经三次"本机绿、CI 红" (W1 ruff 版本、W2 mypy 没进 CI、W5 依赖分档),
#   三次的形状完全一样 —— **本机环境比 CI 富, 所以本机跑绿证明不了 CI 会绿**。
#   对策不是"下次记得多跑一遍", 是让本机能复现 CI 那个更穷的环境。
#   见 docs/ai-development/defect-log.md 案例 5。
#
#   注意 scripts/ci/*.sh 里的 pip install 只在 CI=true 时执行 (lib.sh 的
#   ci_pip_install), 本机直接 `make lint` 是不装依赖的 —— 所以这里显式设 CI=true,
#   让脚本自己按 CI 的方式把依赖装进这个空 venv。这正是"少装了什么"能暴露出来的原因。
#
# 用法:
#   bash scripts/dev/ci-env-repro.sh lint      # ruff + mypy job
#   bash scripts/dev/ci-env-repro.sh unit      # engine job (纯函数, 依赖故意贫瘠)
#   bash scripts/dev/ci-env-repro.sh api       # api job (需要一个真 Postgres)
#
# venv 建在临时目录: 它是一次性探针, 不是交付物, 不进仓库也不该进仓库。
# 想留着复用就设 CI_REPRO_HOME 指到别处。
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

JOB="${1:-}"
case "$JOB" in
  lint) SCRIPT=scripts/ci/lint.sh ;;
  unit) SCRIPT=scripts/ci/test-unit.sh ;;
  api)  SCRIPT=scripts/ci/test-api.sh ;;
  *)
    echo "用法: bash scripts/dev/ci-env-repro.sh <lint|unit|api>" >&2
    exit 2
    ;;
esac

# CI 用的是 3.12 (.github/workflows/ci.yml 的 setup-python)。版本不一致就不是复现。
PY="${CI_REPRO_PYTHON:-python3.12}"
command -v "$PY" >/dev/null || { echo "找不到 $PY —— CI 跑的是 3.12" >&2; exit 1; }

HOME_DIR="${CI_REPRO_HOME:-${TMPDIR:-/tmp}/sentinel-ci-repro}"
VENV="$HOME_DIR/$JOB"

echo "==> 建全新空 venv: $VENV"
rm -rf "$VENV"
"$PY" -m venv "$VENV"

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> CI=true bash $SCRIPT  (依赖由脚本自己装, 装的就是 CI 会装的那些)"
CI=true bash "$SCRIPT"

echo ""
echo "==> $JOB job 复现通过。venv 留在 $VENV, 不用了就 rm -rf 掉。"
