# SPEC-003 · W2 事故生命周期与 RFID 接单

状态: 已实现 (W2), 含 2026-08-06 实现后评审的修订 (见文末修订记录)

## 目标

把 W1 的"事件流"升级成"事故流": 传感器变湿 → 自动开事故 → 分配 → 现场刷卡接单 →
解决, 全程有不可篡改的时间线。取代原系统只有 `spill_events` 点记录、无生命周期的做法。
本 SPEC 只覆盖后端闭环; 登录鉴权见 SPEC-004, 前端面板见 SPEC-005。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/incidents` | 列表, 可按 `status` / `zone_id` 过滤, 默认按 `opened_at` 倒序 |
| GET | `/incidents/{id}` | 单条 + 完整 `incident_events` 时间线 |
| POST | `/incidents/{id}/assign` | `{employee_id, allow_cross_zone?}` → `assigned`; 允许改派 |
| POST | `/incidents/{id}/acknowledge` | 手工接单 → `acknowledged` |
| POST | `/incidents/{id}/resolve` | `{note}` → `resolved` |

**RFID 接单不新增接口**: 复用 W1 的 `POST /ingest` (`kind=rfid_scan`)。
理由 —— 真实硬件是把刷卡当遥测事件发上来的, 另开接口会让模拟器与真机行为分叉,
违反 CLAUDE.md 不变量 3 (DSL/模拟器能力边界一致) 的同一精神。

## 状态机

```
          assign                acknowledge              resolve
  open ───────────► assigned ──────────────► acknowledged ────────► resolved
    │                  │                         ▲                     ▲
    │                  └── assign (改派)          │                     │
    └───────────────────────────────────────────┘                     │
             刷卡直接接单 (未预先分配也允许)                             │
                                                                       │
  任意未解决状态 ── 传感器连续干燥超过阈值 ──────────────────────────────┘
              (W3 起由 Policy 引擎的 sensor_dry_for 触发器判定,
               resolved_by = policy:{policy_id}@v{version})
```

- `resolved` 是终态, 不可回退 (需要重开就开新事故, 保证时间线不被改写)。
- **`assigned` 可以再次 assign** = 改派。主管派错人时不必等对方接单 (修订 3)。
  改派写 `incident_events(kind='reassigned')`, detail 带前后两个 employee_id。
- 每次流转在同一事务里写 `incident_events`(事实时间线) + `audit_log`(谁在什么时候做了什么)。
- 非法流转返回 **409**, 且推进用条件更新 `UPDATE ... WHERE status IN (允许的旧状态)`,
  受影响行数为 0 即判定冲突 —— 并发下两个人同时点"解决"只会有一个成功。

## 关键决策

1. **谁来开事故**: W2 先用硬编码规则「`sensor_state` 转湿即开事故」, 写在
   `services/incident_service.py`。W3 由 Policy 引擎接管, 届时这段规则整体删除, 不做兼容层。
   **已于 W3 第二段兑现** (SPEC-006 第四节): `apply_sensor_state()` 整体删除, 开单与关单
   都归策略; 只拆出一个不做任何判断、只记时间线的 `record_sensor_observation()` ——
   删的是判断逻辑, 保留的是事实记录 (决策 2、决策 4 的两类时间线因此不受影响)。
2. **去重**: 同一 `sensor_id` 已存在未解决事故时**不再新开**, 只追加一条
   `incident_events(kind='sensor_still_wet')`。用 partial unique index 在数据库层兜底
   (`WHERE status <> 'resolved'`), 与 W1 的幂等思路一致 —— 约束下沉到 DB, 不靠应用层自觉。
3. **"派给谁"与"谁实际接的单"是两个字段** (修订 1):
   - `assigned_employee_id` —— 主管指派的人;
   - `acknowledged_by_employee_id` —— 实际推进到 acknowledged 的人 (刷卡人或手工接单人)。

   起因: 原实现用 `COALESCE(assigned_employee_id, 刷卡人)` 保留派单人, 信息本身没丢
   (时间线里有), 但列表接口返回的 `assigned_employee_name` 会让前端显示成
   "Alex 已接单", 而实际到场的是 Bo。两个字段各记各的, 前端不会显示错, 也能统计
   "派单命中率"(派给谁 == 实际谁接的比例)这类指标。
   `open → acknowledged` 跳过分配时, `assigned_employee_id` 保持为空, 不回填。
4. **传感器持续干燥后自动解决, 但记录解决来源**。`incidents.resolved_by`:
   人工解决记 `employee:{id}` 或 `user:{id}`, 自动解决记
   **`policy:{policy_id}@v{version}`** (W3 起; W2 那版记的是笼统的 `auto_sensor_dry`)。
   新口径信息更多 —— 能追到是哪条策略的哪一个版本关的单。
   **区分人工与自动的判据相应改为前缀**: 以 `policy:` 开头即自动, 以 `employee:` /
   `user:` 开头即人工。下面"指标不失真"那条按新判据执行, 用途不变。
   - 取舍: 水干了不等于处理完 (可能只是蒸发、也可能是间歇性滴漏暂时停了),
     要求全人工则演示流程每次都得手点。区分来源后两边都成立。
   - **指标不失真**: "平均处理时长"只统计 `resolved_by` 以 `employee:` 开头的那批;
     自动解决的单独统计成"无人响应自愈率" —— 这个数本身就是有价值的运营指标,
     W5 的 Evals 面板分开展示。
   - **稳定窗口**: 要求该传感器连续一段时间没有再报湿才触发, 否则读数在阈值附近抖动时
     事故会反复开关。W2 用全局配置 `SENTINEL_AUTO_RESOLVE_DRY_SECONDS` (默认 300);
     **W3 起改由 Policy DSL 的 `sensor_dry_for` 触发器的 `dry_for_s` 参数表达**,
     该配置项已删除。好处是可以按区配不同的值 —— 后场冷库地面本就潮, 与卖场中区
     不该用同一个数。
   - 每次转干仍记一条 `incident_events(kind='sensor_dry')`, 无论是否达到自动解决条件。
5. **`actor` 字段先占位**: SPEC-004 的 JWT 落地前, 手工接口的 actor 取
   `X-Actor` 请求头, 缺省 `"system"`; JWT 接入后改成从 token 取, 接口签名不变。
6. **刷卡找不到人 / 找不到事故**: 刷卡事件照常落 `rfid_scans` 表 (遥测事实不丢),
   但不推进任何事故, 返回体里带 `matched: false` + 原因码, 供前端提示"未登记的卡"。
7. **派单按区域约束, 刷卡不按区域约束** —— 这个不对称是刻意的 (修订 2、修订 3):
   - **派单 (assign)**: 默认只能派给 `employees.zone_id == incidents.zone_id` 的人,
     否则返回 **422**。要跨区必须显式传 `allow_cross_zone=true`,
     此时照常派单, 但审计日志额外记 `cross_zone: true` 与双方 zone_id ——
     "谁在什么时候跨区派了单"成为一个可查询的事实, 而不是一条没人看的日志。
     员工 `zone_id` 为空视为不属于任何区域, 同样需要 `allow_cross_zone`。
   - **刷卡 (rfid_scan)**: 完全不校验刷卡人属于哪个区, 只按**刷卡设备所在区域**
     取该区最早开的未解决事故。
   - 理由: 安排工作时按区域分工, 紧急情况谁在现场谁上。约束派单是为了责任清晰,
     不约束刷卡是为了现场不被流程卡住。

## 数据库

`incidents` / `incident_events` / `audit_log` 三张表 W1 的 `0001_initial.sql` 已建好;
`assigned_at` / `resolved_by` 与三个索引在 `0003_incidents` 已加。

本次修订新增一个迁移 (`make migrate-new id=0004_incident_ack_by m="..."`):

- `incidents.acknowledged_by_employee_id bigint REFERENCES employees(id)` —— 决策 3。

新增配置项 `apps/api/app/config.py`: `auto_resolve_dry_seconds: int = 300` (已有)。

迁移写法遵循 ADR-006: 手写, 不用 autogenerate (本项目无 ORM 模型); 必须写 downgrade。

## 验收

- 跑 `make sim` 触发漏水场景 → `/incidents` 出现一条 `open`;
- 同一传感器持续报湿 → 不新增事故, 时间线累加 `sensor_still_wet`;
- `POST /ingest` 一条 `rfid_scan`(用种子数据里 Alex 的卡 `04A1B2C3`) → 该事故变 `acknowledged`
  且时间线记录了是谁刷的卡;
- **派给 Alex 后由 Bo 刷卡接单** → `assigned_employee_id` 仍是 Alex,
  `acknowledged_by_employee_id` 是 Bo, 两者都能从 `/incidents/{id}` 读到;
- **未派单直接刷卡** → `assigned_employee_id` 为空, `acknowledged_by_employee_id` 是刷卡人;
- **跨区派单默认被拒** → 给 1 区事故派 2 区的 Bo 返回 422;
- **带 `allow_cross_zone=true` 跨区派单成功**, 且 `audit_log` 里能查到 `cross_zone: true`;
- **改派**: 已 `assigned` 的事故再次 assign 成功, 时间线出现 `reassigned` 且带前后两人;
- 人工调 `/resolve` → `resolved_by = 'employee:3'`;
- 传感器转干并保持超过阈值 → 事故自动变 `resolved` 且 `resolved_by` 形如
  `policy:{id}@v{n}` (W3 起; W2 那版是 `auto_sensor_dry`);
- 传感器转干但未满阈值又转湿 → 事故仍未解决, 不产生关单;
- 对 `resolved` 的事故再调 `/resolve` 返回 409;
- 并发两次 `/resolve` 只有一个 200, 另一个 409;
- 测试命名遵循 `test_<行为>__<条件>`;
- `make lint` 与全部测试绿。

## 不在本 SPEC 范围

JWT/RBAC (SPEC-004)、React Dashboard (SPEC-005)、事故升级策略 (W3 Policy 引擎)、
事故报告生成 (W5 Agent)。

## 修订记录

**2026-08-06, 实现后逐行评审代码时发现并确认:**

1. **拆出 `acknowledged_by_employee_id`** —— 原实现保留派单人 (COALESCE), 语义正确
   但会让前端把"派给谁"显示成"谁接的单"。改成两个字段各记各的。
2. **确认派单与刷卡的区域约束不对称**, 并把这一点从"实现细节"提升为显式决策 (决策 7),
   因为它是会被追问的设计选择, 不该只存在于代码里。
3. **允许改派** —— 原实现 `assign` 限定 `WHERE status = 'open'`, 派出去就改不了。
   放宽为 `status IN ('open','assigned')`, 改派单独记 `reassigned` 事件。
