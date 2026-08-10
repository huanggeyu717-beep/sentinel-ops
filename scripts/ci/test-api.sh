#!/usr/bin/env bash
# API 测试, 需要一个真实 Postgres。
# 地址由 SENTINEL_TEST_DATABASE_URL 指定; 不设则用 conftest 里的本机默认值。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_pip_install -r apps/api/requirements.txt -r requirements-dev.txt
ci_pip_install -e packages/policy_engine
ci_pip_install -e packages/scenario

# evals/runner/tests 也归这一档 (W5): 那两个文件 import httpx / asyncpg, 而这个 job
# 本来就装了 apps/api 全套依赖。它们不连库 (用 MockTransport 与手造数据),
# 放这里纯粹是因为**依赖装得上**——分档的依据是依赖, 不是要不要 Postgres。
section "pytest apps/api/tests evals/runner/tests"
pytest apps/api/tests evals/runner/tests -q
