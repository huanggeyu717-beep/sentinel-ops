# ADR-006: 迁移改用 Alembic, 但保留"启动时自动升级"

日期: 2026-08 | 状态: 已接受

## 背景

W1 用的是自己写的极简迁移器: 按文件名顺序执行 `migrations/*.sql`, 用 `schema_migrations`
台账保证只跑一次。它解决了"三条路径共用同一套建表逻辑"的问题, 但缺三样东西:
版本图 (谁的上一个是谁)、downgrade、以及行业通用性。
按文件名排序意味着两个人各加一个 `0003_` 不会冲突报错, 而是静默乱套。

## 决策

1. **W2 起由 Alembic 管理迁移**, 版本图代替文件名排序。
2. **基线不翻写**: `0001_baseline` 直接执行 W1 那份 `0001_initial.sql` 原文
   (只去掉显式的 BEGIN/COMMIT, 因为 Alembic 自己已经开了事务)。
   翻成 `op.create_table(...)` 要手抄 24 张表, 抄漏一个约束不会有任何报错。
   已验证: 全新库跑 Alembic 建出的 schema, 与 W1 老迁移器建出的逐字一致。
3. **老库平滑接管**: 启动时若发现"有 `schema_migrations` 台账、但没有 `alembic_version`",
   就按台账里最后一个文件 stamp 对应版本, **不执行任何建表语句**。
   开发机和队友已经建好的库不需要重来。
   旧台账表保留不删 —— 删表若与 stamp 不在同一事务, 中途失败会退化成"两边都没有版本记录",
   下次启动会试图从零建表并撞车。留着是无害的历史痕迹。
4. **保留启动时自动升级**, 不改成命令行 `alembic upgrade head`。
   官方教程都教后者, 但那会让"只有 Docker 里能建表", 本地裸跑和 CI 又得各写一套。
   Alembic 是同步库, 因此在 `asyncio.to_thread` 里跑, 不阻塞事件循环。
5. **迁移用同步驱动 psycopg, 应用运行时仍走 asyncpg**。
   asyncpg 的扩展查询协议不支持一次执行多条语句, 而基线就是多语句 SQL;
   换同步驱动后 `env.py` 保持成标准模板的样子, 命令行 upgrade/downgrade 也直接可用。
   代价是镜像里多一个驱动 (约 5MB)。

## 已知边界

- **autogenerate 不可用**。本项目业务代码写原生 SQL, 没有 SQLAlchemy ORM 模型,
  没有模型就没有"比对"的对象。所有迁移手写。这是选原生 SQL 时就付的账, 不是遗漏。
- **基线不支持 downgrade**。回滚基线等于清空整个库, 语义上应该 `make reset` 重建,
  而不是伪装成一次降级。`0002` 之后的迁移都要写 downgrade。
- Dockerfile 必须 COPY `alembic.ini` + `alembic/` + `migrations/` 三者,
  少任何一个容器启动即失败 —— 已在 Dockerfile 里就地注释说明。
