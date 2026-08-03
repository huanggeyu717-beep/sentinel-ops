# SPEC-000 · W1 事件入口与状态查询

状态: 已实现 (W1)

## 目标

在没有硬件的前提下建立系统唯一的数据源与事实存储: 模拟器产生事件 → `/ingest` 规范化落库 →
`/status` 可查。W2 的事故生命周期、W3 的策略引擎、W5 的评测 fixture 全部构建在这条链路上。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针 (compose healthcheck 与 sim 探活都用它) |
| POST | `/ingest` | 单条事件; `kind ∈ {sensor_state, heartbeat, rfid_scan}` |
| POST | `/ingest/batch` | 同事务批量 (≤1000 条), 历史回放用 |
| GET | `/status/sensors` | 各传感器当前状态 + 所属区域 + age_seconds |
| GET | `/status/devices` | 设备心跳, `online = age ≤ 60s` (沿用 legacy 语义) |
| GET | `/status/readings` | 最近读数, 可按 sensor_id 过滤 |
| GET | `/status/summary` | 四张表计数, 冒烟与演示用 |

事件报文字段与原系统 MQTT 消息一一对应 (反推自 legacy `database-ingest` Lambda),
因此未来接回真实硬件只需替换一个 MQTT bridge, 不动业务代码。

## 关键决策

1. **幂等键 = (device_id, 业务标识, ts)**, 由唯一索引在数据库层保证 (`0002_ingest_idempotency.sql`)。
   模拟器重放、前端重试、未来 MQTT 重投都不会产生重复行。
2. **乱序保护**: `sensorstate` / `device_heartbeats` 的 UPSERT 带 `WHERE 旧.ts < 新.ts`。
   原 Lambda 是无条件覆盖, 迟到的旧报文会把"已恢复"的状态改回"漏水"——这是一处实质缺陷修复。
3. **建表走 API 启动时的迁移器, 不走 `docker-entrypoint-initdb.d`**。
   理由: 本地裸跑、CI、Docker 三条路径共用同一建表逻辑, 避免"只有 Docker 里能建表"。
   W2 起换 Alembic, 届时把已应用文件登记为 baseline。
4. **时间轴语义显式化**: 加速回放时, 事件时间戳默认随播放压缩(保证 `/status` 的 age/online 有意义);
   `--scenario-ts` 保留原始间隔, 供 W3 验证"3 分钟内"这类策略时间窗。两者不可兼得, 故做成开关。

## 验收

- `docker compose up --build` 后 `/health` 返回 200, 迁移与种子自动完成;
- `python sim.py seed/waterlevel_readings.csv --batch` 把队友仓库导出的 344 条真实读数
  (+ 按仿真时间补的心跳) 灌入库, 重复执行 `stored` 为 0;
- `GET /status/sensors` 返回 5 个传感器的当前状态且带区域名;
- `pytest apps/api/tests apps/device-sim` 全绿 (19 条用例, 覆盖幂等/乱序/阈值推导/校验拒绝/在线判定)。
