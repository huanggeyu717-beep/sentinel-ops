#!/usr/bin/env bash
# API 测试, 需要一个真实 Postgres。
# 地址由 SENTINEL_TEST_DATABASE_URL 指定; 不设则用 conftest 里的本机默认值。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_pip_install -r apps/api/requirements.txt -r requirements-dev.txt
ci_pip_install -e packages/policy_engine
ci_pip_install -e packages/scenario

section "pytest apps/api/tests"
pytest apps/api/tests -q
