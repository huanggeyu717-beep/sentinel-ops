.PHONY: help up down logs sim sim-basic replay test test-api test-unit lint lint-fix lint-version dev-tools evals psql reset

help:           ## 列出所有命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:             ## 起全套 (db + api + web)
	docker compose up --build

down:           ## 停掉全部容器
	docker compose down

logs:           ## 跟踪 API 日志
	docker compose logs -f api

sim:            ## 回放"同区多传感器无人响应升级"场景 (x10 加速)
	python apps/device-sim/sim.py apps/device-sim/scenarios/multi_sensor_escalation.yaml --speed 10

sim-basic:      ## 回放单点漏水基础流程 (x10 加速)
	python apps/device-sim/sim.py apps/device-sim/scenarios/basic_spill.yaml --speed 10

replay:         ## 把原系统真实历史读数一次性灌入数据库 (幂等, 可重复执行)
	python apps/device-sim/sim.py apps/device-sim/seed/waterlevel_readings.csv --batch

# 以下四个目标调用的是 scripts/ci/ 下与 CI 完全同一份脚本, 不是"抄了一遍"。
test:           ## 全部测试 (device-sim + policy_engine + api)
	bash scripts/ci/test-unit.sh
	bash scripts/ci/test-api.sh

test-unit:      ## 只跑不依赖数据库的测试
	bash scripts/ci/test-unit.sh

test-api:       ## 只跑 API 测试 (需要本地 Postgres)
	bash scripts/ci/test-api.sh

lint:           ## 静态检查 (先校验 ruff 版本, 再跑 CI 同一份脚本)
	bash scripts/ci/lint.sh
	mypy apps/api packages/policy_engine

lint-fix:       ## 自动修可修的 lint 问题
	ruff check --fix packages apps/api apps/device-sim evals

lint-version:   ## 确认本机 ruff 版本与 requirements-dev.txt 锁定的一致 (见 ADR-005)
	@bash scripts/ci/check-tool-versions.sh

dev-tools:      ## 安装/对齐本机工具链版本 (macOS 系统 Python 需要 --break-system-packages, 已自动兜底)
	pip3 install -r requirements-dev.txt \
		|| pip3 install -r requirements-dev.txt --break-system-packages

psql:           ## 进数据库命令行
	docker compose exec db psql -U sentinel -d sentinel

reset:          ## 清空数据库卷重来 (慎用)
	docker compose down -v

evals:
	python evals/run_evals.py --arm A2 --dataset evals/datasets/policies_v1.jsonl
