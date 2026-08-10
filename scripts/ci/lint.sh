#!/usr/bin/env bash
# 静态检查。CI 与本机是同一个文件 —— 这是"本机绿 = CI 绿"的结构性保证。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# 检查目标是 lib.sh 里的 LINT_TARGETS —— ruff、mypy、make lint-fix 同一份, 见那里的注释。
# 本脚本额外接一个 --fix: make lint-fix 走这条路, 免得它再抄一份目标清单。
FIX=""
[ "${1:-}" = "--fix" ] && FIX="--fix"

# mypy 要看见被检查代码的**运行时依赖**才能检查它: 没装 fastapi / sqlalchemy /
# httpx / asyncpg 时, mypy 对每一条 import 报 import-not-found 直接 exit 1 ——
# 不是"少检查一点", 是**一条都没检查成**。
#
# 这就是 W5 那次 CI 红的真根因, 而且它从 W2 把 mypy 放进 CI 那天起就一直红:
# lint.sh 当时挂在 engine job 上, 而那个 job 故意只装 requirements-dev.txt。
# 所以 lint 从进 CI 起就没有绿过一次, 也就从没拦下过任何东西 ——
# 又一个"看起来在守、实际守空气"。
#
# 因此 lint 独占一个 job (ci.yml), 装全量依赖; engine job 保持依赖贫瘠,
# 那份贫瘠正是"单元档真的离线"这个保证的来源, 不能被 lint 的安装污染。
ci_pip_install -r apps/api/requirements.txt -r requirements-dev.txt

section "ruff check ${FIX} ${LINT_TARGETS[*]}"
if [ -n "$FIX" ]; then
  bash scripts/ci/check-tool-versions.sh
  ruff check --fix "${LINT_TARGETS[@]}"
  # --fix 只是本机的便利入口, 不跑 mypy (它没有可自动修的东西), 到此为止
  exit 0
elif [ "${CI:-}" = "true" ]; then
  # --output-format=github: 报错直接标注到 PR 的对应代码行上
  ruff check --output-format=github "${LINT_TARGETS[@]}"
else
  bash scripts/ci/check-tool-versions.sh
  ruff check "${LINT_TARGETS[@]}"
fi

# mypy 以前只在本机 make lint 里跑, CI 完全不跑 —— 等于它拦不住任何东西。
# 放进这里之后, ruff 和 mypy 两道检查在本机与 CI 执行的是同一份脚本。
# (但 W2 到 W5 之间它在 CI 上一次都没绿过, 原因见上面那段 pip install 的注释:
#  "放进 CI" 与 "在 CI 上真的跑得起来" 是两件事。)
# evals 是 W5 加的: 不列进目标的话, mypy.ini 里 evals.graders.* 的严格档白名单
# 是空转的 —— 白名单只对被扫描的文件生效 (与"检查目标漏加"同一类问题)。
# mypy 与 ruff 现在共用 LINT_TARGETS: 原来 mypy 逐个点名两个包、还漏掉
# apps/device-sim 与 scripts/dev, 两套口径迟早对不上 (见 lib.sh 那段注释)。
section "mypy ${LINT_TARGETS[*]}"
mypy "${LINT_TARGETS[@]}"
