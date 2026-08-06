"""Alembic 运行环境。

两点与默认模板不同, 都是刻意的:

1. **数据库地址从 app.config 读**, 不写在 alembic.ini 里。
   应用连哪个库、迁移改哪个库, 只能有一个来源。
2. **迁移走同步驱动 psycopg, 应用运行时仍走异步驱动 asyncpg**。
   迁移是启动时一次性的动作, 用同步驱动能让 env.py 保持成标准模板的样子,
   也让 `alembic upgrade head` / `downgrade` 在命令行直接可用;
   代价是镜像里多一个驱动。取舍见 docs/adr/ADR-006-alembic-migrations.md。

本项目没有 SQLAlchemy ORM 模型 (业务代码写原生 SQL), 所以 target_metadata 为 None,
autogenerate 不可用 —— 迁移一律手写。这是清醒的选择, 不是遗漏。
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
target_metadata = None


def _sync_url() -> str:
    """把应用的异步 DSN 转成 Alembic 用的同步 DSN。"""
    from app.config import settings

    return settings().database_url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    """离线模式: 只生成 SQL, 不连库 (`alembic upgrade head --sql`)。"""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
