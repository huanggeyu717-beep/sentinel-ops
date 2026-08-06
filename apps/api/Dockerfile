# 构建上下文是仓库根目录(见 docker-compose.yml), 因为 API 依赖 packages/ 下的本地包
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 依赖层单独 COPY, 改业务代码时不会让 pip 重跑
COPY apps/api/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY packages/policy_engine ./packages/policy_engine
RUN pip install ./packages/policy_engine

# 演练接口 (SPEC-005) 复用模拟器的场景装载, 场景剧本本身在仓库根 scenarios/。
# 漏 COPY scenarios 的话, drill_service 导入时就会报错, 容器起不来 —— 刻意的快炸。
COPY packages/scenario ./packages/scenario
RUN pip install ./packages/scenario
COPY scenarios ./scenarios

COPY apps/api/app ./app
# 迁移相关: alembic.ini + 版本脚本 + W1 那两份被基线引用的 .sql 原文。
# 少 COPY 任何一个, 容器启动时的自动升级都会失败 (见 ADR-006)。
COPY apps/api/alembic.ini ./alembic.ini
COPY apps/api/alembic ./alembic
COPY apps/api/migrations ./migrations

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
