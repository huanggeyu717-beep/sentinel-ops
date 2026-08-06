# SPEC-005 · W2 Dashboard 第一页

状态: 已评审, 待实现 (W2 待办 4)

## 目标

第一个人类能看见的界面: 平面图 + 传感器状态 + 设备心跳 + 事故列表与时间线 + 演练面板。
它同时是 W4 演示视频的主场景 —— "触发漏水 → 事故出现 → 派单 → 刷卡接单 → 解决"
这条链要能全程在网页里走完, 不用切终端。

前置是 SPEC-004 (登录与权限)。本 SPEC 依赖它的**接口契约**而非实现, 因此可以并行设计。

## 两个后端前置

Dashboard 的五块里, 三块 (传感器状态 / 心跳 / 事故列表) 复用现成接口;
另外两块各缺一段后端, 都在本 SPEC 范围内:

### 前置 A: 位置数据进数据库

`zones` / `devices` / `sensors` 三张表现在**一个位置字段都没有**。原系统把
`sensorZoneMap` 写成前端常量, 刷新即丢 —— 那正是本项目要修的毛病之一, 不能重蹈。

迁移 `0006_positions`:
- `sensors.pos_x numeric(5,2) NULL` / `sensors.pos_y numeric(5,2) NULL`
- `devices.pos_x` / `devices.pos_y` 同上

**坐标是相对底图的百分比 (0–100), 不是像素也不是经纬度。** 这是室内平面图不是地理地图;
用百分比则底图换分辨率、换比例都不必改数据。可空: 新装但还没标位置的设备照样能入库,
前端把无坐标的设备列在图外的"未定位"区。

设备与传感器分别存位置, 因为物理上不在一处 (板子在墙上, 探头在地面)。

### 前置 B: 演练触发接口

模拟器 `sim.py` 现在只能命令行跑。演示视频里要能在网页上点一下就触发场景。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/drills/scenarios` | 列出 `apps/device-sim/scenarios/*.yaml` 可用场景 |
| POST | `/drills/{scenario}` | 启动一次演练, 202 + `{drill_id}` |
| GET | `/drills/{drill_id}` | 该次演练的状态与进度 |

**前端一律不自己造事件。** 直接调 `/ingest` 发假数据会绕过模拟器, 破坏
"模拟器是唯一事实源"这个前提 (SPEC-000 决策 4 与 CLAUDE.md 不变量 3 的同一精神)。

## 页面结构

单页, 上下两栏:

- **上栏 · 平面图**: 底图 + 传感器点 (湿=告警色 / 干=正常色 / 失联=灰) + 设备点 (在线/离线)。
  点一个传感器, 右侧抽屉出它的最近读数与所属事故。
- **下栏左 · 事故列表**: 状态、区域、派给谁、谁接的单、开始时间。点开是完整时间线
  (`/incidents/{id}` 已返回 events)。列表内可做 assign / acknowledge / resolve 三个操作。
- **下栏右 · 演练面板**: 场景下拉 + 启动按钮 + 当前演练进度。
- **顶栏**: 当前用户与角色、退出登录。

## 关键决策

1. **实时更新用轮询, 不用 SSE**。传感器状态 5 秒、事故列表 5 秒、演练进度 2 秒。
   `sse-starlette` 虽已装好, 但留给 W4 的 Agent 执行过程 —— 那里才真需要"一步步冒出来"
   的效果。第一页用轮询足够, 且少一类连接状态要维护。
2. **前端按角色隐藏按钮, 但这不是安全措施**。viewer 看不到派单/解决按钮, 是体验优化;
   **真正的拦截在服务端** (SPEC-004 决策 6)。SPEC 里写死这一条, 是为了防止后来者
   误以为"前端藏了就安全了" —— 用 curl 绕过前端是三秒钟的事。
   验收里有一条: 用 viewer 的会话直接 curl 打 assign, 必须 403。
3. **不存 token**。SPEC-004 用 httpOnly cookie, 前端不碰令牌, 只需 `fetch` 带
   `credentials: 'include'`。收到 401 就跳登录页。
4. **演练任务放内存, 不进 `agent_tasks` 表**。`agent_tasks` 是给需要审计、重试、
   死信的 Agent 任务用的 (ADR-003); 演练是演示用的一次性动作, 丢了重跑即可。
   刻意不复用, 免得把一张有审计语义的表稀释成通用任务表。
   代价: API 重启后进行中的演练状态丢失 —— 可接受, 且要在响应里说清楚。
5. **同一场景不允许并发演练**。已有同名演练在跑时返回 409。
   否则两份事件流交叉灌进同一批传感器, 时间线会变得没法解释。
6. **触发演练需要 operator 及以上**。viewer 只能看不能点。

## 演练接口如何复用模拟器 (已定: 方案 B)

`sim.py` 在 `apps/device-sim`, API 在 `apps/api`。把公共部分抽成新包 `packages/scenario/`,
两边都依赖它。选 B 而不是"API 起子进程跑 sim.py", 是因为 **W3 的历史数据模拟还要再用
一次同样的能力** —— 现在抽一次, 比先凑合再返工划算; 也避免把"api 容器里塞一份模拟器"
这个别扭永久留在 Dockerfile 里。

**职责切分**:

| 归属 | 内容 |
|---|---|
| `packages/scenario/` | 读场景 (YAML 剧本 / CSV 回放) → 规范化事件流; 时间轴换算 |
| `apps/device-sim/sim.py` | 命令行参数、按 `--speed` 推进时间、HTTP 投递、进度打印 |
| API 的 drill 服务 | 复用上面的事件流, 按仿真时间投递 |

**新包的 IO 边界**: 允许读场景文件, **禁止发网络请求、禁止碰数据库**。
这条要写进包的模块 docstring —— 否则以后一定有人往里塞一个 HTTP 客户端,
它就退化成第二个模拟器了。 (`packages/policy_engine` 是零 IO, 标准更严;
两个包的边界不同, 不要混为一谈。)

**drill 投递事件时走哪条路**: 不自己拼 SQL, 也不 HTTP 自调, 而是**调用与 `/ingest` 路由
同一个 service 函数, 并复用同一个 pydantic 请求模型做校验**。这样演练与真机的校验逻辑
是同一份, 只是少了 HTTP 传输那一跳。

### 抽包的连带改动 (容易漏, 逐条核)

1. 新建 `packages/scenario/pyproject.toml` (依赖 PyYAML);
2. `apps/device-sim/sim.py` 改为依赖新包, 命令行行为保持不变
   (`make sim` / `make sim-basic` / `make replay` 三条命令的输出不变);
3. `apps/device-sim/Dockerfile` 与 `apps/api/Dockerfile` 都要 COPY 并安装新包
   —— 参照 api 现在装 `policy_engine` 的写法;
4. `scripts/ci/test-unit.sh` 与 `test-api.sh` 里的 `ci_pip_install -e packages/policy_engine`
   要加一行装 `packages/scenario`;
5. `pytest.ini` 的 `pythonpath` 加 `packages/scenario`;
6. `mypy.ini` 严格档白名单加 `scenario.*` (新包一开始就该是严格档, 别留债);
7. device-sim 现有 6 条测试要么跟着搬到新包, 要么保持在原处但导入新包 —— 两种都行,
   但**测试总数不能减少**。

## 验收

- 未登录访问首页跳转登录页; 登录后进入 Dashboard;
- 平面图上 5 个传感器按当前状态着色, 失联设备显示为灰;
- 无坐标的设备出现在"未定位"区而不是消失;
- 点传感器出抽屉, 显示最近读数与关联事故;
- 事故列表能完成 assign → acknowledge → resolve, 每步之后列表自动刷新;
- 事故详情显示完整时间线, 且**"派给谁"与"谁接的单"分列两栏** (SPEC-003 修订 1);
- viewer 登录后看不到操作按钮, 且用 curl 直接打 assign 返回 403;
- 演练面板选场景启动后, 平面图上的传感器在 30 秒内变色、事故列表出现新事故;
- 同一场景重复启动返回 409;
- `npm run typecheck` 与 `npm run build` 通过 (CI 的 web job 已在跑这两条);
- 抽包之后 `make sim` / `make sim-basic` / `make replay` 三条命令行为不变, 输出不变;
- `make lint` 与全部测试绿, 且**测试总数不少于抽包前**。

## 不在本 SPEC 范围

Policy 编辑与 Automation Studio (W4)、Agent 执行过程展示 (W4)、Evals 面板 (W5)、
移动端适配、国际化。
