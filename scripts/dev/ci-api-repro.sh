#!/usr/bin/env bash
# 复现 CI 的 api job —— 本机一条命令证明 api job 会绿 (make ci-api-repro)。
#
# 为什么 `make test` 跑绿证明不了: 三层差异 (viewer401 那次红暴露的完整形状,
# 见 docs/ai-development/W6-CI修复-viewer401-完成报告.md):
#   1. scripts/ci/*.sh 在本机故意不装依赖 —— 没进 venv 就跑, 报出来的是
#      ModuleNotFoundError, 看起来像代码坏了;
#   2. 本机的 sentinel_test 是常驻库, CI 每次是全新库 —— viewer401 就藏在
#      这个差异里 (id 收敛的巧合只在常驻库上成立);
#   3. 本机没有别的命令跑得完 CI api job 的同一份脚本。
#
# 所以这里: venv 检查 -> DROP sentinel_test -> 跑与 CI 完全同一份 test-api.sh。
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 前置检查先行: 让"没进 venv"报成一句人话, 不报 ImportError
if ! python -c "import asyncpg" >/dev/null 2>&1; then
  echo "本机跑请先 source .venv/bin/activate (当前 python import 不到 asyncpg)" >&2
  exit 1
fi

TEST_URL="${SENTINEL_TEST_DATABASE_URL:-postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel_test}"

python - "$TEST_URL" <<'PY'
import asyncio
import sys

import asyncpg


async def go(url: str) -> None:
    dsn = url.replace("+asyncpg", "")
    admin_dsn, db_name = dsn.rsplit("/", 1)
    conn = await asyncpg.connect(admin_dsn + "/postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        print(f"已 DROP {db_name} (与 CI 同条件: 全新库)")
    finally:
        await conn.close()


asyncio.run(go(sys.argv[1]))
PY

bash scripts/ci/test-api.sh
