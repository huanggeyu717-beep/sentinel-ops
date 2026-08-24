#!/usr/bin/env bash
# eval 回归冒烟: 拿进版本库的那份轨迹离线复跑, 守判分 / runner / 统计口径的改动。
# SPEC-007 第五节 (回归臂"CI 只跑这个") + 验收 5 (注入得逞率不足 0 则退出码非零)。
#
# **零 token 成本**: replay 模式一次真实调用都不发, cassette 全在版本库里。
#
# 为什么挂在 api job 而不是 engine job:
#   runner 按 SPEC-007 第七节走 HTTP —— 它要起一个真的 API 子进程、要一个真的
#   Postgres (每臂重置评测库)、要 httpx 与 asyncpg。engine job 三样都没有,
#   而它**故意**没有: 那份依赖贫瘠正是"单元档真的离线"这个保证本身
#   (defect-log 案例 5)。为了跑冒烟往里装 asyncpg, 等于把刚修好的东西再拆一次。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SMOKE_DIR=evals/cassettes/smoke

# api job 里本脚本排在 test-api.sh 之后, 依赖已经装好; 单独跑时这行兜底
ci_pip_install -r apps/api/requirements.txt -r requirements-dev.txt
require_modules_outside_ci asyncpg httpx

# 评测库与 API 库分开 (SPEC-007 第七节)。CI 上指向同一个 Postgres 服务的另一个库;
# 本机不设时走 config 默认值 (5433 上的 sentinel_eval)。
if [ "${CI:-}" = "true" ] && [ -z "${SENTINEL_EVAL_DATABASE_URL:-}" ]; then
  export SENTINEL_EVAL_DATABASE_URL="postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel_eval"
fi

section "eval 回归冒烟 (replay, 零调用零花费)"
LOG="$(mktemp)"
set +e
python evals/run_evals.py \
  --arm L2 --mode replay \
  --cassette-dir "$SMOKE_DIR" \
  --cases-file "$SMOKE_DIR/cases.json" 2>&1 | tee "$LOG"
RUNNER_RC="${PIPESTATUS[0]}"
set -e

# 归档目录从输出里取。CI 上这份归档是一次性的 (容器随后就没了), 本机跑完删掉 ——
# 冒烟的归档不是任何人会去读的结果, 留着只会把 evals/runs/ 弄脏。
RUN_DIR="$(sed -n 's/^归档: //p' "$LOG" | tail -1)"
rm -f "$LOG"

if [ -z "$RUN_DIR" ] || [ ! -d "$RUN_DIR" ]; then
  echo "没找到本次冒烟的归档目录 —— runner 多半在起 API 之前就挂了 (退出码 $RUNNER_RC)" >&2
  exit 1
fi

# 判据不用 runner 自己的退出码: 那个码是为真跑的五臂设计的, 非注入类失败时返回 0。
# 详见 evals/runner/smoke.py 的模块注释。
section "校验冒烟判据 (十条全过 / 零回放 miss / 零注入得逞)"
set +e
python -m evals.runner.smoke "$RUN_DIR" "$SMOKE_DIR/cases.json"
CHECK_RC=$?
set -e

rm -rf "$RUN_DIR"

if [ "$RUNNER_RC" -ne 0 ] || [ "$CHECK_RC" -ne 0 ]; then
  echo "冒烟回归失败 (runner 退出码 $RUNNER_RC / 判据退出码 $CHECK_RC)" >&2
  exit 1
fi
