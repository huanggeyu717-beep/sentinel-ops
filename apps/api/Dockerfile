# 构建上下文是仓库根目录(见 docker-compose.yml), 因为 API 依赖 packages/policy_engine
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

COPY apps/api/app ./app
COPY apps/api/migrations ./migrations

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
