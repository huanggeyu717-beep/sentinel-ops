-- W1: 事件入口幂等约束
-- 背景: /ingest 可能被模拟器重放、被 MQTT bridge 重投、被前端重试触发。
-- 约定幂等键 = (device_id, 业务标识, 事件时间戳), 冲突时 DO NOTHING / 只接受更新的时间戳。
BEGIN;

-- 同一设备的同一传感器在同一毫秒只会有一条读数
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_device_sensor_ts
    ON waterlevel_readings (device_id, sensor_id, received_ts);

-- 同一设备的同一张卡在同一毫秒只会有一次刷卡
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfid_device_uid_ts
    ON rfid_scans (device_id, rfid_uid, scan_ts);

-- 状态/心跳查询走时间倒序
CREATE INDEX IF NOT EXISTS ix_readings_received_at ON waterlevel_readings (received_at DESC);
CREATE INDEX IF NOT EXISTS ix_rfid_scanned_at ON rfid_scans (scanned_at DESC);

COMMIT;
