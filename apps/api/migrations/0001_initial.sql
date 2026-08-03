-- Sentinel 初始 schema (W1 用原生 SQL 起步, W2 起切 Alembic 管理)
-- 左列注释 = 该表取代的原系统位置
BEGIN;

-- ===== 身份与权限 (原: 前端硬编码 APP_PASSWORD='demo' + Cognito 备份未接线) =====
CREATE TABLE users (
    id            bigserial PRIMARY KEY,
    email         text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    display_name  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE roles (id serial PRIMARY KEY, name text UNIQUE NOT NULL); -- viewer/operator/manager/admin
CREATE TABLE user_roles (
    user_id bigint REFERENCES users(id),
    role_id int REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

-- ===== 库存 (原: app.js 里的 zones/employees/sensorZoneMap 常量, 刷新即丢) =====
CREATE TABLE zones (id serial PRIMARY KEY, name text NOT NULL);
CREATE TABLE devices (
    id serial PRIMARY KEY, name text NOT NULL, zone_id int REFERENCES zones(id),
    installed_at timestamptz DEFAULT now()
);
CREATE TABLE sensors (
    id serial PRIMARY KEY, device_id int REFERENCES devices(id),
    zone_id int REFERENCES zones(id), active boolean NOT NULL DEFAULT true,
    threshold_value int NOT NULL DEFAULT 500
);
CREATE TABLE employees (
    id serial PRIMARY KEY, name text NOT NULL, role text NOT NULL,
    email text NOT NULL, zone_id int REFERENCES zones(id), rfid_uid text UNIQUE
);

-- ===== 遥测 (沿用原表设计, 加索引) =====
CREATE TABLE waterlevel_readings (
    id bigserial PRIMARY KEY, received_at timestamptz NOT NULL, received_ts bigint NOT NULL,
    device_id text NOT NULL, sensor_id int NOT NULL, value int, wet boolean, raw jsonb
);
CREATE INDEX ON waterlevel_readings (sensor_id, received_ts DESC);
CREATE TABLE sensorstate (
    sensor_id int PRIMARY KEY, wet boolean NOT NULL, state text NOT NULL,
    updated_ts bigint NOT NULL, updated_at timestamptz NOT NULL, last_value int
);
CREATE TABLE device_heartbeats (
    device_id text PRIMARY KEY, last_seen_at timestamptz NOT NULL,
    last_seen_ts bigint NOT NULL, uptime_ms bigint, raw jsonb
);
CREATE TABLE rfid_scans (
    id bigserial PRIMARY KEY, device_id text NOT NULL, rfid_id text NOT NULL,
    rfid_uid text NOT NULL, scan_ts bigint NOT NULL, scanned_at timestamptz NOT NULL, raw jsonb
);

-- ===== 事故生命周期 (原系统只有 spill_events 点记录, 无生命周期) =====
CREATE TABLE incidents (
    id bigserial PRIMARY KEY,
    zone_id int REFERENCES zones(id), sensor_id int,
    severity text NOT NULL DEFAULT 'normal' CHECK (severity IN ('normal','high','critical')),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','assigned','acknowledged','resolved')),
    assigned_employee_id int REFERENCES employees(id),
    opened_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz, resolved_at timestamptz
);
CREATE TABLE incident_events (
    id bigserial PRIMARY KEY, incident_id bigint REFERENCES incidents(id),
    kind text NOT NULL, actor text, detail jsonb, at timestamptz NOT NULL DEFAULT now()
);

-- ===== 策略 =====
CREATE TABLE policies (
    id bigserial PRIMARY KEY, name text NOT NULL,
    enabled boolean NOT NULL DEFAULT false, created_by bigint REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE policy_versions (
    id bigserial PRIMARY KEY, policy_id bigint REFERENCES policies(id),
    version int NOT NULL, body jsonb NOT NULL,              -- Policy DSL
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','validated','simulated','published','rejected','rolled_back')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (policy_id, version)
);
CREATE TABLE policy_runs (   -- 线上触发历史, Trace/审计用
    id bigserial PRIMARY KEY, policy_version_id bigint REFERENCES policy_versions(id),
    fired_at timestamptz NOT NULL, effects jsonb NOT NULL
);

-- ===== Agent 编排 =====
CREATE TABLE agent_tasks (
    id bigserial PRIMARY KEY, user_id bigint REFERENCES users(id),
    task_type text NOT NULL,                                -- policy_compile / incident_report
    input jsonb NOT NULL, stage text NOT NULL DEFAULT 'parsing',
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','awaiting_approval','clarifying','completed',
                          'failed','rejected','dead_letter')),
    attempt_count int NOT NULL DEFAULT 0, idempotency_key text UNIQUE,
    error_code text, created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz
);
CREATE TABLE agent_steps (   -- Trace UI 的事实源
    id bigserial PRIMARY KEY, task_id bigint REFERENCES agent_tasks(id),
    seq int NOT NULL, tool_name text NOT NULL, arguments jsonb,
    result_summary jsonb, status text NOT NULL, latency_ms int, retry_count int DEFAULT 0,
    input_tokens int, output_tokens int, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE approvals (
    id bigserial PRIMARY KEY, task_id bigint REFERENCES agent_tasks(id),
    policy_version_id bigint REFERENCES policy_versions(id),
    requested_by bigint REFERENCES users(id), decided_by bigint REFERENCES users(id),
    decision text CHECK (decision IN ('approved','rejected')), decided_at timestamptz
);

-- ===== 用量 / 评测 / 审计 =====
CREATE TABLE ai_usage (
    id bigserial PRIMARY KEY, task_id bigint, model text NOT NULL, prompt_version text,
    input_tokens int, output_tokens int, estimated_cost_usd numeric(10,6),
    latency_ms int, cache_hit boolean DEFAULT false, at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE eval_cases (
    id text PRIMARY KEY, category text NOT NULL, input jsonb NOT NULL,
    expected jsonb NOT NULL, scenario_ref text
);
CREATE TABLE eval_runs (
    id bigserial PRIMARY KEY, arm text NOT NULL, model text NOT NULL,
    prompt_version text, git_sha text, started_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE eval_results (
    run_id bigint REFERENCES eval_runs(id), case_id text REFERENCES eval_cases(id),
    passed boolean, failure_kind text, detail jsonb, latency_ms int, tokens int,
    PRIMARY KEY (run_id, case_id)
);
CREATE TABLE audit_log (  -- 原: 前端 DOM 表格 + 手工下载 CSV
    id bigserial PRIMARY KEY, user_id bigint, action text NOT NULL,
    entity text, entity_id text, detail jsonb, at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO roles (name) VALUES ('viewer'), ('operator'), ('manager'), ('admin');
COMMIT;
