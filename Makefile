.PHONY: help up up-bg down logs sim sim-basic replay test test-api test-unit lint lint-fix lint-version ci-repro ci-lint-repro ci-unit-repro dev-tools migrate migrate-status migrate-down migrate-new evals eval-db-reset psql reset

help:           ## 列出所有命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:             ## 起全套 (db + api + web), 占用当前窗口刷日志, Ctrl+C 停
	docker compose up --build

up-bg:          ## 同上但转后台, 窗口可继续用; 看日志用 make logs, 停用 make down
	docker compose up --build -d

down:           ## 停掉全部容器
	docker compose down

logs:           ## 跟踪 API 日志
	docker compose logs -f api

sim:            ## 回放"同区多传感器无人响应升级"场景 (x10 加速)
	python apps/device-sim/sim.py scenarios/multi_sensor_escalation.yaml --speed 10

sim-basic:      ## 回放单点漏水基础流程 (x10 加速)
	python apps/device-sim/sim.py scenarios/basic_spill.yaml --speed 10

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

lint:           ## 静态检查 ruff + mypy (与 CI 同一份脚本)
	bash scripts/ci/lint.sh

lint-fix:       ## 自动修可修的 lint 问题
	ruff check --fix packages apps/api apps/device-sim evals

lint-version:   ## 确认本机 ruff 版本与 requirements-dev.txt 锁定的一致 (见 ADR-005)
	@bash scripts/ci/check-tool-versions.sh

# 本机 venv 比 CI 富, 所以 make lint / make test 跑绿**证明不了 CI 会绿** ——
# 本项目已经为此栽了三次 (defect-log 案例 5)。下面三个目标在全新空 venv 里
# 按 CI 的方式装依赖再跑, 是本机唯一能证明 CI 会绿的办法。
ci-repro:       ## 在全新空 venv 里复现 CI 的 lint + engine 两个 job
	bash scripts/dev/ci-env-repro.sh lint
	bash scripts/dev/ci-env-repro.sh unit

ci-lint-repro:  ## 只复现 lint job (ruff + mypy, 装全量依赖)
	bash scripts/dev/ci-env-repro.sh lint

ci-unit-repro:  ## 只复现 engine job (纯函数档, 依赖故意贫瘠 —— 坑都在这里)
	bash scripts/dev/ci-env-repro.sh unit

dev-tools:      ## 安装/对齐本机工具链版本 (macOS 系统 Python 需要 --break-system-packages, 已自动兜底)
	pip3 install -r requirements-dev.txt \
		|| pip3 install -r requirements-dev.txt --break-system-packages

migrate:        ## 把数据库升到最新版本 (启动 API 时也会自动执行, 这里是手动入口)
	cd apps/api && alembic upgrade head

migrate-status: ## 看当前版本与迁移历史
	cd apps/api && alembic current && alembic history

migrate-down:   ## 回滚一步 (基线不可回滚, 要清库用 make reset)
	cd apps/api && alembic downgrade -1

migrate-new:    ## 新建迁移: make migrate-new id=0003_incidents m="incidents 索引与 assigned_at"
	@test -n "$(id)" || (echo "缺 id, 例: make migrate-new id=0003_incidents m=\"说明\""; exit 1)
	@test -n "$(m)"  || (echo "缺 m, 例: make migrate-new id=0003_incidents m=\"说明\""; exit 1)
	cd apps/api && alembic revision --rev-id "$(id)" -m "$(m)"

psql:           ## 进数据库命令行
	docker compose exec db psql -U sentinel -d sentinel

reset:          ## 清空数据库卷重来 (慎用)
	docker compose down -v

evals:          ## 评测 dry-run (零调用零花费); 真跑要显式 --mode record --max-cost-cny
	python evals/run_evals.py --arm L0 --mode record --dry-run

eval-db-reset:  ## 重置评测库 (drop + create + 迁移 + dev seed + inventory 一致性校验)
	python evals/run_evals.py --reset-db
