# SPEC-001 · W3 Policy DSL v1 与双层验证器

状态: 已定稿 (2026-08-07 开工前评审重写)。原版是 W1 之前的草稿, 与 W3 的实际目标对不上,
本次整篇重写。评审发现的问题与取舍见文末**评审记录**。

本 SPEC 只覆盖**策略语言本身**与**验证它的两层检查**。
版本化、审批、发布、引擎接管开关事故、Automation Studio 后端接口见 **SPEC-006**。

## 目标

给自然语言运营规则一个受限的编译目标。管理员说「生鲜区两个探头三分钟内都湿了就通知
这个区的主管」, 最终落成一段结构化的 Policy, 存进 `policy_versions.body`。

**为什么不让模型直接产出可执行代码**: 那等于把任意副作用权限交给模型。DSL 是一个封闭
白名单 —— 动作只有五个, 条件只有两类, 通知目标只能从角色枚举里选而不接受自由字符串。
模型**在语法层面就说不出**白名单以外的话。这也是 `CLAUDE.md` 不变量 3 的由来:
正因为这门语言能表达的东西有限, 模拟器才可能把所有情况跑一遍;
换成通用代码, "先模拟一遍看看它会干什么"在理论上就不可能。

## 非目标 (v1 明确不做, 理由随附)

- **自由代码执行、跨策略编排**: 见上, 与整个设计前提冲突。
- **非邮件通知渠道**: v2。
- **`time_window` 时间窗条件**: **本次砍掉**。引擎是纯函数、只吃 epoch 毫秒, 没有时区
  信息; 跨午夜 (22:00–06:00) 语义也没定义。它还是引擎里唯一一个要引入外部时钟的东西,
  会破坏"同样输入必得同样输出"的可复现性 —— 而可复现性正是模拟器有价值的前提。
  对漏水这个场景收益也低。要做就得先给 Policy 加显式 IANA 时区字段, 留 v2。
- **`operator_on_duty` (当班操作员) 这个通知目标**: 系统里**没有任何排班数据**,
  谁在当班无从知道。写进 DSL 是一个语法上说得出、语义上兑现不了的空承诺。
  v1 通知某角色时通知该角色**全部在册员工**; 排班表留 v2。
- **策略之间的优先级与互斥消解**: 引擎按确定性顺序产出 Effect, 冲突由执行器用幂等
  处理 (见"多策略同时命中"一节), 不引入优先级字段。

---

## 一、引擎的输入: 事件流

引擎是纯函数, `evaluate()` 只消费事件流与自身状态, 不做任何 IO (`CLAUDE.md` 不变量 2)。
**事件流是引擎唯一的输入** —— 这一条本次升格为显式不变量, 因为原设计在这里是断的:
`incident_unacknowledged` 这类条件需要"事故存不存在、有没有人接单"这些数据库里的事实,
而原来的 `Event` 只有三类遥测事件, 这些事实没有任何进入引擎的通道。

### 事件类型 (八类)

| kind | 来源 | 关键字段 |
|---|---|---|
| `sensor_state` | `/ingest` 遥测 | `sensor_id`, `zone_id`, `device_id`, `state` (WET/DRY) |
| `heartbeat` | `/ingest` 遥测 | `device_id` |
| `rfid_scan` | `/ingest` 遥测 | `device_id`, `rfid_uid` |
| `tick` | 引擎时钟 (**新增**) | 无 |
| `incident_opened` | `incident_service` (**新增**) | `incident_id`, `sensor_id`, `zone_id`, `device_id` |
| `incident_assigned` | `incident_service` (**新增**) | `incident_id` |
| `incident_acknowledged` | `incident_service` (**新增**) | `incident_id` |
| `incident_resolved` | `incident_service` (**新增**) | `incident_id` |

`incident_opened` **必须带 `device_id`**: 否则由事故唤醒的策略产不出 `set_led`
(它需要知道点哪台设备的灯), 而引擎零 IO 反查不了。引擎另外从 `sensor_state` 事件
学习 `sensor_id → device_id` 与 `device_id → zone_id` 两张映射作为补充。

后四类**由 `incident_service` 在写库的同一个事务里投递**, 与状态变更同生共死:
事务回滚则事件不存在, 引擎看到的与数据库里的永远一致。
投递机制见 SPEC-006。

**zone_id 的唯一事实源是数据库** (`sensors.zone_id`)。`/ingest` 规范化事件时从库里补上,
不采信报文里的值。场景 YAML 里手写的 `zone_id` 只在纯离线模拟 (不连库) 时生效。
两处不一致时以数据库为准 —— 原系统把 `sensorZoneMap` 写成前端常量正是本项目要修的毛病。

### tick: 为什么必须有

纯事件驱动的引擎**表达不了"经过了多久还没发生事"**。"事故开了两分钟没人接单"这件事,
在那个时刻没有任何事件发生, 引擎不会被唤醒。设备离线同理 —— 离线的定义就是"没有消息",
而"没有消息"永远不会触发任何东西。

所以引擎的输入里必须有一个固定间隔的时钟事件。

- 间隔由 `SENTINEL_ENGINE_TICK_SECONDS` 配置, **默认 10 秒**。
- 线上由一个后台异步任务产出; 模拟器按**仿真时间**产出**同样间隔**的 tick。
- **两边间隔必须一致**, 否则模拟结果对线上没有预测力。间隔值写进模拟报告的元数据,
  让任何一次回放结果都能被复现。
- 模拟器的时间轴是 `[0, 最后一个事件时刻 + tail_s]`, `tail_s` **默认 600 秒**。
  没有这条尾巴, "持续干燥 5 分钟后关单"这类规则在场景末尾永远验证不到 ——
  最后一个事件之后就没有 tick 了。

**已知边界**: tick 后台任务在多实例部署下会重复产出。W3 是单实例, 记在 SPEC-006
的已知边界里; W6 上线时若扩多实例需要选主。

---

## 二、Policy 的结构

```
Policy
  scope       作用范围            见下
  trigger     什么事件唤醒它       一个, 四选一
  conditions  唤醒后还要满足什么    0 到 8 个
  actions     满足后做什么         1 到 4 个
  cooldown_s  冷却秒数            60 到 86400
```

**`name` 不在 DSL 里**。名字归 `policies` 表那一列。理由: 名字是**策略这个实体**的属性,
不是**某个版本**的属性 —— 改条件通常不改名, 只改名也不该凭空产生一个新版本。
两处各存一份必然走散, 而走散的那份从外面看不出来 (W2 已经踩过一次: 底图存了两份,
实际渲染的是组件里那份, `.svg` 那份没人引用也没人发现)。

**`requires_approval` 不在 DSL 里**, 由服务端从动作分级推导, 永不信任模型输入
(`CLAUDE.md` 不变量 5)。所有模型都是 `extra="forbid"`, 模型若自作主张塞这个字段,
在 Schema 层直接失败。

### scope: 这条策略管哪里

```
scope:
  type: global | zone | sensor
  ids:  [int]        # type=global 时必须为空; 其余必须非空, 最多 16 个
```

原设计没有这个字段, 想限定区域得往条件数组里塞一个 `zone_in`。三个问题:

1. **表达不了"只管 3 号传感器"** —— 条件里只有 `zone_in`, 没有对应的 sensor 版本。
   而平面图上刻意放在**下游**的那个探头, 行为与上游几个不同, 很可能要单独一条策略。
2. **"这条策略管哪里"没有单一答案** —— 界面要显示作用范围, 得把 conditions 数组翻一遍;
   这段逻辑会在前端、后端、验证器各写一遍, 三份必然走散。
3. **和 `wet_sensor_count` 里那个同名字段撞词** —— 顶层的"管哪些地方"与条件内部的
   "数传感器时数同区的还是所有区的"是两个层次的东西, 却都叫 scope。

引入 scope 后: `zone_in` 条件**删除**; `wet_sensor_count.scope` 改名 `count_within`。

引擎在事件进来的第一步就用 scope 筛掉不相关的策略, 不必把每条策略的条件都算一遍。

### trigger: 什么事件唤醒它 (四类)

"必定提供"= 该 trigger 触发时这个字段一定有值; "条件性提供"= 视运行时状态而定。

| type | 参数 | 由什么驱动 | 必定提供 | 条件性提供 |
|---|---|---|---|---|
| `sensor_state_changed` | `to: WET \| DRY` | 遥测事件, **边沿触发** | sensor_id, zone_id, device_id | incident_id (该传感器有未解决事故时) |
| `device_offline` | `offline_for_s: 30..3600` | tick | device_id | zone_id (该设备上报过 sensor_state 时) |
| `incident_elapsed` | `in_status: open \| assigned \| acknowledged`<br>`for_s: 30..7200` | tick | incident_id, sensor_id, zone_id, device_id | — |
| `sensor_dry_for` | `dry_for_s: 60..7200` | tick | sensor_id, zone_id, device_id | incident_id (该传感器有未解决事故时) |

**边沿触发**: `sensor_state_changed` 只在状态**发生变化**时触发 (DRY→WET 或 WET→DRY),
持续报同一状态不重复触发。沿用原 automatic-alert Lambda 的语义。

**"提供的上下文"这两列是有约束力的**, 静态验证器用它做检查 (第五节
`E_CONTEXT_UNAVAILABLE`)。判定规则**必须写死**, 否则实现者会自行发挥:

- 动作需要的字段落在"**必定提供**"里 → 通过;
- 落在"**条件性提供**"里 → **静态检查通过**(静态阶段无从判断运行时有没有事故),
  但运行时若该字段为空, **该 Effect 不产出**, 并在 `policy_runs` 里记一条
  `skipped: missing_context` —— 静默丢弃是不可接受的;
- 两列都没有 → `E_CONTEXT_UNAVAILABLE`, 提交草稿那一刻就拦住。

**tick 驱动的 trigger 是边沿触发**: 只在条件从"不满足"变为"满足"的那个 tick 触发一次,
之后持续满足不重复触发, 直到条件再次变为不满足。

**边沿状态按 `(policy_id, 触发主体)` 分桶**, 触发主体是该 trigger 天然作用的对象:

| trigger | 触发主体 |
|---|---|
| `sensor_state_changed` | sensor_id |
| `device_offline` | device_id |
| `incident_elapsed` | **incident_id** |
| `sensor_dry_for` | sensor_id |

**注意这个键与 cooldown 的键不是同一个** (cooldown 按 `scope` 的作用对象分桶, 见第四节)。
两者是两个独立机制, 用途也不同: 边沿判定防的是"同一个主体每个 tick 都触发",
cooldown 防的是"同一个作用范围内短时间反复产出"。混成一个键会让第七节验收 6
那条对照测试直接失效 —— 事故 1 和事故 2 是两个不同的主体, 各有各的边沿,
但可能落在同一个冷却桶里。

**`incident_elapsed` 的时长从事故进入该状态的时刻起算**, 不从引擎观察到它的那个 tick
起算 (两者可能差一个 tick)。引擎为此维护一个 `status_since`。

初稿写的是"一律从 `incident_opened` 的 `ts_ms` 起算", **已在第一段实现后评审时改掉**:
那个口径对 `in_status=open` 没问题, 但 `in_status=acknowledged, for_s=30` 会变成
"事故开了 30 秒以上、且当前是已接单", 而不是"已接单 30 秒" —— 与字面意思相反。
当初只想着 open 那一档。

`sensor_state_changed` 的边沿另有一条要写明: **传感器第一次上报即视为一次状态变化**
(此前没有"上一个状态"可比)。首报 WET 触发 `to=WET` 的策略 —— 沿用原
automatic-alert Lambda 的语义, 且场景包里的传感器都是首报即 WET,
不这样整条链路开不了工。

### conditions: 唤醒后还要满足什么 (两类)

```
wet_sensor_count
  count_within: same_zone | any_zone
  op:           ">=" | "==" | "<="
  value:        1..32
  window_s:     10..3600
```

**语义定死为**: 统计**此刻正湿着**的传感器数量, 且这些传感器的**变湿时刻必须落在同一个
`window_s` 窗口内**。`count_within=same_zone` 时只数与触发对象同区的传感器。

原设计这里有两种读法, SPEC 没选, 代码里两种暗示都有 (`wet_since` 像甲, `window_s` 像乙):

- 甲: 此刻正湿着的有几个
- 乙: 过去 `window_s` 里曾经变湿过的有几个

用 `scenarios/multi_sensor_escalation.yaml` 就能看出差异。1 号 t=5s 变湿、2 号 t=40s 变湿,
两个一直湿到 t=260 和 t=290。在 **t=200s** 这一刻:

- 按甲: 两个都还湿着 → 计数 2 → 满足 `>=2`
- 按乙: 窗口是 t=20 到 t=200, 1 号的变湿事件发生在 t=5 **已经掉出窗口** → 计数 1 → 不满足

同一份数据、同一条策略, 两个相反的结论。**必须写死**, 否则实现者会随便挑一个,
而读代码的人看不出它挑的是哪个 —— 两种写法看起来都合理。

选甲的理由: 符合直觉 ("现在有两个地方在漏水"), 也符合真实场景 —— 水顺地面坡度与地漏跑,
几个探头先后变湿但会**持续**湿着, 这正是把一个探头放在下游的用意。
实现上引擎也最省, 只需维护 `wet_since[sensor_id]` 一个字典。

**窗口是不锚定的**: 取历史上任意位置、能盖住最多变湿时刻的那个 `window_s` 长的窗口,
**不要求触发对象自己落在窗口里**, 也不锚定在当前时刻 (锚定当前时刻就退回读法乙了)。
所以"两个探头在三分钟内相继变湿"这个事实, 只要两个探头一直没干, 一天之后依然成立。
这是刻意的 —— 它描述的是"这一摊水是不是同一次漏出来的", 与它已经漏了多久无关。

```
incident_unacknowledged
  duration_s: 30..7200
```

关联事故已处于 `open` 或 `assigned` (即无人 acknowledge) 超过 `duration_s`。

**与 `incident_elapsed` trigger 的分工**: trigger 决定"什么时候唤醒", condition 是
"唤醒之后的额外约束"。典型用法 —— trigger 是"传感器又湿了", condition 是"而且已有事故
超过五分钟没人接", 动作是升级等级。两者参数看着像, 位置和职责完全不同。
(这个用法成立的前提是 `sensor_state_changed` **条件性提供** `incident_id`,
见上面 trigger 表 —— 该传感器没有未解决事故时, 这条策略静态检查照样通过,
运行时不产出 Effect 并记 `skipped: missing_context`。)

**v1 只有两个条件, 是刻意的。** `CLAUDE.md` 不变量 3 要求每加一个能力必须同时提交
schema、语义校验、引擎实现、场景包用例四样。宁可少而完整。

### actions: 做什么 (五类)

| type | 参数 | 审批分级 | 必需上下文 |
|---|---|---|---|
| `open_incident` | `severity: normal \| high \| critical` | internal_write | sensor_id, zone_id |
| `close_incident` | 无 | internal_write | incident_id |
| `escalate_incident` | `to_severity: high \| critical` | internal_write | incident_id |
| `notify` | `channel: email`<br>`target_role: viewer \| operator \| manager \| admin` | external_side_effect | **无** |
| `set_led` | `target: incident_device`<br>`state: ON \| OFF` | external_side_effect | device_id |

`notify` **没有必需上下文**: 它的投递目标是角色, 不是某个具体对象。`subject` 里的
`zone_id` / `incident_id` 有就填 (用于收窄到本区、用于在事故时间线上留痕), 没有也能发。
这样"设备离线就通知管理员"这类策略才写得出来 —— 设备离线时未必知道它属于哪个区。

**`open_incident` 是本次新增, 而且是 W3 能不能做下去的关键。** 原设计只有通知、点灯、
升级三个动作, 可 W3 的任务恰恰是"删掉 `incident_service` 里转湿就开事故的硬编码,
交给策略引擎"。删掉之后没有任何动作能开事故 —— 派单、刷卡接单、Dashboard 事故列表,
W2 整条链路全部空转。而 `escalate_incident` 和 `incident_unacknowledged` 又都预设
"事故已经存在", 事故是谁开的, 原 SPEC 没有答案。

**`close_incident` 也是本次新增。** 原本"传感器报干且距最后一次报湿超过 300 秒就关单"
这条规则硬编码在 Python 里, 与开事故的规则一里一外。搬进 DSL 之后:

- 规则集中在一个地方, 接手的人不必读两处;
- 不同区可以有不同的关单标准 (后场冷库地面本就潮, 与卖场中区不是一回事),
  而原来 `auto_resolve_dry_seconds` 是一个全局配置一刀切;
- **模拟器终于能验证关单行为** —— 回放能给出完整的"开 → 关"轨迹,
  才算得出"平均事故持续时长", W5 的评测面板要用;
- 改"多久算处理完"这个业务判断要走审批并留版本, 而不是改配置发一次版、无人知晓。

`close_incident` 写进 `incidents.resolved_by` 的值是 `policy:{policy_id}@v{version}`,
比原来笼统的 `auto_sensor_dry` 信息更多 —— 能追到是哪条策略的哪一版关的。
SPEC-003 决策 4 "区分人工解决与自动解决"的用途不受影响, 且更精确。相应修订见 SPEC-006。

**通知目标对齐 `roles` 表**。原设计取值是 `zone_manager / operator_on_duty / admin`,
而数据库 `roles` 表是 `viewer / operator / manager / admin`, `employees.role` 又是第三套
自由文本。三套名字互不相干, 结果是验证器那条"这个角色有没有在册员工"的检查里,
`zone_manager` 在任何一张表里都查不到, **每一条带通知的策略都会报错**, 检查直接废掉。
现在统一成 `roles` 表那四个值; "通知本区的 manager"这层意思由 scope 表达
(scope 是 1 区 + 目标是 manager = 通知 1 区的 manager)。

### 审批分级的用途

`ACTION_APPROVAL_CLASS` 把动作分成 `external_side_effect` 与 `internal_write` 两级。
原设计有这张表但**没有任何地方用到它**, SPEC 也没说它意味着什么 —— 一段没有用途的代码,
读的人会以为自己漏看了什么。

**v1 的用途定为**: 一律 manager 审批, 门槛**不因分级而变** (与 SPEC-004 权限表一致);
分级只用来在审批界面上标红提示"这条策略会往外发邮件"。
分级不改变"要不要审批", 但改变"审批时该盯哪里" —— 一条只改事故等级的策略, 与一条
会给全区主管群发邮件的策略, 风险不是一回事。

**必须配一条测试**: 分级表的键集合与动作类型白名单**完全相等**。现在加了新动作却忘了
补分级不会报任何错, 查表返回空, 审批界面什么提示都不显示 —— 一个静悄悄的、漏在审批
环节的洞。让不一致当场变红灯。这个手法本项目已用过三处 (`X-Actor` 零残留 grep、
两个包的 IO 边界 AST 扫描、演练接口的同步性断言), 这是第四处。

---

## 三、引擎的输出: Effect

```
Effect
  ts_ms          事件时间
  policy_id      用 id 不用 name
  policy_version 版本号
  action_type
  subject        作用对象, 见下
  detail         动作参数原样回显
```

```
EffectSubject
  sensor_id:   int | None
  zone_id:     int | None
  device_id:   str | None
  incident_id: int | None
```

**`subject` 是强制的, 各动作的必填项见上面动作表的"必需上下文"一列。**

原设计的 Effect 只有一个自由字典 `detail`, SPEC 没规定里面必须有什么。后果:
执行器拿到一个 `set_led / ON` **不知道点哪台设备的灯**, 拿到 `escalate_incident / critical`
**不知道升哪一条事故**。执行器只能自己去猜、去查数据库 —— 而这一猜就把"引擎是纯函数、
输出完全确定"破坏了: 同一个 Effect 在不同时刻执行会作用到不同对象上。

这还直接决定模拟器有没有预测力。不带对象的模拟结果只能告诉你"会点三次灯", 不能告诉你
"点的是哪三台"。带上之后, 模拟输出可以逐条与线上真实执行记录对照, W5 才能用
"行为轨迹是否一致"判分 (`CLAUDE.md` 里说的"行为等价")。

`incident_id` 从哪来: 从 `incident_opened` 等事故事件来。引擎在内部维护
`incident_id → {sensor_id, zone_id, status}` 与 `sensor_id → 未解决事故 id` 两张表,
全部由事件流喂养, 不查库。

---

## 四、cooldown: 冷却

**冷却的键是 `(policy_id, 作用对象)` 这一对**, 不是策略名。作用对象按 scope 类型取:

| scope.type | 冷却分桶依据 |
|---|---|
| `sensor` | 触发事件的 `sensor_id` |
| `zone` | 触发事件的 `zone_id` |
| `global` | 全策略共用一个桶 |

原实现 `last_fired: dict[str, int]` 用**策略名**做键, 两个问题:

1. **严重**: 1 区刚触发过、还在冷却期, 这时后场漏水**会被吞掉**。引擎只记了"这条策略上次
   几点触发", 不管是哪个区。对"防邮件风暴"尚可忍受, 对**开事故绝对不行** ——
   后场的水没开出事故, 理由是十分钟前生鲜区漏过一次。这是会真出事的那一类。
2. 名字是可随时修改的展示字段。管理员把"生鲜区告警"改成"生鲜区漏水告警", 冷却记录立刻
   对不上, 等于冷却被悄悄重置。身份要用 id。

**冷却抑制的是"产出 Effect", 不是"跳过判断"。** 冷却期内, trigger 照常匹配、conditions
照常求值、引擎内部状态 (`wet_since` / 事故表等) 照常更新, 只是不往外吐 Effect。
若连判断都跳过, 滑动窗口状态会断, 冷却一结束的第一次判断就会算错。

含 `notify` 动作的策略, `cooldown_s` 下限 **300 秒** (静态验证器 `E_COOLDOWN_TOO_SHORT`)。

**命中即计入冷却, 哪怕全部动作都因缺上下文被跳过。** 冷却防的是"命中频率",
不是"产出数量" —— 若按产出计, 一条动作总被跳过的策略会每个 tick 都重新命中,
把 `skipped` 刷成一片。

### 引擎状态必须有上界

`EngineState` 里几个随时间累积的容器 —— `incidents` / `tick_edge` / `last_fired` ——
**必须有明确上界**, 不能"开过多少单就留多少条"。这是本项目已经立过的规矩
(SPEC-005 决策 4: "内存里的东西必须有上界", 演练历史照此只留最近 N 次), 这里同样适用。

**淘汰规则**:

- **未解决的事故一条都不淘汰。** 它们的数量**数据库已经封住了** —— partial unique
  index 保证同一传感器最多一条未解决事故, 上界就是传感器数。
- **已解决的事故保留最近 N 条** (`SENTINEL_ENGINE_INCIDENT_HISTORY`, 默认 200),
  超出丢最旧。已解决的事故**没有任何触发器还会用到它**: `incident_elapsed` 的
  `in_status` 只有 open / assigned / acknowledged, `resolved` 不在其中;
  `incident_unacknowledged` 对已解决的事故本来就该判否。保留一小批只是为了
  迟到事件与排查方便。
- 事故被淘汰时, **它名下的 `tick_edge` 条目一并清理** —— 否则换了个容器继续无上界。

  **`last_fired` 不需要清理**, 因为它根本不按事故分桶: 冷却的键是
  `(policy_id, scope 作用对象)`, 而 scope 只有 sensor / zone / global 三种,
  所以 `last_fired` 的上界本来就是"策略数 × (传感器数 + 区域数 + 1)" ——
  随库存规模变化, 不随开过多少单变化。
  (本节初稿写的是"tick_edge 与 last_fired 一并清理", 是我把两个键的分桶依据搞混了;
  第二段修补时由实现者指出并更正。)

- **事故主体的 `tick_edge` 键必须带命名空间前缀** (如 `incident:{id}`)。
  裸整数在正常运行时不会出问题 (一条策略只有一个 trigger, 主体空间是齐的),
  但**淘汰时的清理是跨策略扫描**的 —— 淘汰 5 号事故时, 若某条 `sensor_dry_for`
  策略恰好有个 5 号传感器的边沿记录, 就会被误删。这是加淘汰逻辑才引入的风险,
  加前缀是最省事的堵法。

**清掉内存里的事故不丢任何历史**: 事实源在数据库 (`incidents` / `incident_events` /
`policy_runs`), 引擎状态只是为了判断"该不该触发"而攒的工作台账。
这一句要写进 `EngineState` 的 docstring, 免得后来者以为动它会丢数据而不敢清。

顺带一个性能后果: 线上 `tick()` 每轮会对引擎状态做一次快照 (见 SPEC-006 第四节的
失败回退), 且 `incident_elapsed` 每个 tick 都要遍历事故表。两者的开销都应当随
"当前有多少未解决事故"变化, 而不是随"历史上开过多少单"变化。
无上界时实测: 5000 个完整生命周期之后, 单次快照就要 36 毫秒, 而 tick 每 10 秒一次。

### 多策略同时命中

引擎**不引入优先级字段**。同一个事件命中多条策略时:

- Effect 按 `(事件时序, policy_id)` **稳定排序**输出, 同样的输入必得同样的顺序;
- **冲突消解交给执行器, 靠幂等而不是靠仲裁**。`open_incident` 撞上同一传感器已有未解决
  事故时是空操作 (partial unique index 在数据库层兜底, 与 W2 一致);
  `close_incident` 作用于已 resolved 的事故时是空操作;
  两条策略一条 `set_led ON` 一条 `set_led OFF` 时, 后一条生效 —— 这属于策略写错了,
  应当由**静态验证器的跨策略检查**在发布前拦住, 而不是让引擎在运行时猜。

跨策略冲突检测放在 SPEC-006 的发布前检查里 (需要读到其它已发布策略, 超出纯函数边界)。

---

## 五、第一层: 静态验证器

Schema 层由 Pydantic 承担 (白名单外的 type 直接失败)。这一层是**语义**检查。

输出**结构化错误码 + path + message + hint**。Agent 的修复循环靠错误码而不是自然语言,
这也是 W5 评测里"参数正确率 / 修复成功率"可度量的前提。

### Inventory (验证所需的资源快照, 由 service 层提供)

```
zone_ids:      frozenset[int]
sensor_ids:    frozenset[int]
sensor_zone:   dict[int, int]      # sensor_id -> zone_id, same_zone 检查要用
roles_present: frozenset[str]      # 见下
```

**`roles_present` 的事实源定死为 `user_roles`**, 即"当前有哪些角色下挂着账号"。

不能用 `employees.role`: 那一列是无约束的自由文本, 与 `roles` 表没有任何关联
(见 `0001_initial.sql`), 拿它去比对 `roles` 表的枚举值必然对不上 —— 这正是本次要修的
"三套角色名"问题的根源, 不能一边修一边又踩回去。

代价: 检查的是"有没有这个角色的账号", 不是"有没有这个角色的现场员工"。
`notify` 的实际投递目标 (W6 接真实邮件出口时) 也按 `users` 走,
需要按区收窄时经 `users.employee_id → employees.zone_id` (SPEC-004 已建的那条链)。
**scope 是某个区、但该区没有对应角色的人**这种情况 v1 不做静态检查, 记在这里,
理由是它需要遍历 users×employees×zones 三张表, 收益不抵复杂度, 且不会导致误判 ——
最坏结果是一封通知没人收, 而这已经由 `E_ROLE_NOT_STAFFED` 覆盖了大部分。

### 错误码

| 码 | 触发条件 |
|---|---|
| `E_UNKNOWN_ZONE` | `scope.ids` 里有不存在的 zone |
| `E_UNKNOWN_SENSOR` | `scope.ids` 里有不存在的 sensor (**新增**) |
| `E_SCOPE_IDS_MISMATCH` | `type=global` 却给了 ids, 或 `type=zone/sensor` 却没给 (**新增**) |
| `E_ROLE_NOT_STAFFED` | `notify.target_role` 当前无在册员工 |
| `E_COOLDOWN_TOO_SHORT` | 含 notify 且 `cooldown_s < 300` |
| `E_DUPLICATE_ACTION` | 动作数组里有完全相同的两项 |
| `E_ALWAYS_TRUE_CONDITION` | 条件恒为真 (见下方修正) |
| `E_CONTEXT_UNAVAILABLE` | 动作需要的上下文, 该 trigger 提供不了 (**新增**) |
| `E_SELF_TRIGGER_LOOP` | 动作产生的事件类型能再次唤醒本策略 (**新增**) |

**`E_CONTEXT_UNAVAILABLE` 是本次最有价值的新增检查。** 对照第二节两张表:
每个 trigger 声明了它能提供哪些上下文, 每个 action 声明了它需要哪些。
比如 trigger 是 `device_offline` (没有事故上下文) 而动作里有 `close_incident`
(需要 incident_id), 这条策略**在运行时必然无事可做**。这类错误原本要等到线上跑了才发现,
现在在提交草稿的那一秒就能拦住, 并且能给出可执行的修复提示。

**`E_SELF_TRIGGER_LOOP`**: `open_incident` 会产生 `incident_opened` 事件, 若本策略的
trigger 是 `incident_elapsed`, 就构成自触发环。v1 只做这一层直接环检测,
跨策略的间接环 (A 的动作唤醒 B, B 的动作唤醒 A) 放 SPEC-006 的发布前检查。

**`E_ALWAYS_TRUE_CONDITION` 的既有缺陷要一并修**: 现实现是
`op == "<=" and value >= len(inv.sensor_ids)`, 但 `count_within=same_zone` 时该比的是
**该区的**传感器数, 不是全部。用 `sensor_zone` 按区统计后再比。

---

## 六、第二层: 动态验证 (历史数据回放)

静态验证回答"写得对不对", 动态验证回答"**它在真实数据上会干什么**"。

拿历史上真实发生过的数据把策略从头跑一遍。数据源两类:

- `scenarios/*.yaml` —— 手写剧本, 用于验证特定行为;
- `apps/device-sim/seed/waterlevel_readings.csv` —— **344 条队友仓库导出的真实读数**。

**IO 边界**: 回放模块住在 `packages/policy_engine` 里, 所以它**不读文件** ——
装载场景交给 `packages/scenario` (那个包允许读场景文件), 回放模块只接收已经装载好的
事件列表。零 IO 这条边界不因为"回放看起来像个工具"就放宽。

### 事故投影器: 模拟侧的假执行器

场景文件里能写的只有三类遥测事件 (`sensor_state` / `heartbeat` / `rfid_scan`) ——
它描述的是**设备发生了什么**, 不该也无法描述"系统开了一个事故"。
可是 `incident_elapsed`、`incident_unacknowledged`、`close_incident` 全都依赖事故事件。
线上这些事件由 `incident_service` 投递, **而模拟侧没有 `incident_service` 可用**
(它在 `apps/api` 下, 且要连数据库)。

解决: 回放模块内置一个**事故投影器**, 把引擎产出的 Effect 与遥测事件反过来投影成事故事件,
回灌进事件流:

| 触发 | 投影出的事件 |
|---|---|
| `open_incident` Effect | `incident_opened` (incident_id 用递增序号) |
| `close_incident` Effect | `incident_resolved` |
| `rfid_scan` 事件 | `incident_acknowledged` —— 取该设备所在区最早的未解决事故, 与 SPEC-003 决策 7 的规则一致; 找不到则不投影 |

投影器是**纯函数、零 IO**, device→zone 映射从已经过去的 `sensor_state` 事件里学。

**回灌必须在下一个 tick 才被消费**, 不能在本轮 Effect 收集过程中递归调用引擎。
这条与线上执行器的规则一致 (SPEC-006 第四节), 两边行为因此对齐 ——
这正是"执行器与模拟器是同一份 `evaluate()`"这句话的真正含义:
不只是共用一个函数, 而是连"副作用什么时候变成新的输入"这个时序也一样。

**已知的近似**: 投影器只模拟事故生命周期里与策略相关的那几步, 不模拟派单、不模拟
数据库的 partial unique index。所以模拟里"同一传感器已有未解决事故时 `open_incident`
是空操作"这条要在投影器里显式实现, 否则模拟会比线上多开事故。
这一条要写在投影器的 docstring 里。

一条策略写好之后回放, 就能告诉审批人"这条策略在过去这段真实数据上会开出 7 个事故、
发出 7 封邮件"。管理员本意若是"偶尔漏水才告警", 看到 7 这个数字立刻知道条件写松了。

### 输出

```
ReplayReport
  source           场景名或 CSV 文件名
  events_count     喂进去多少条事件
  span_s           喂入事件的时间跨度 (不含 tail)
  tick_seconds     复现所需
  tail_s           复现所需
  effects          完整 Effect 序列 (含时刻与作用对象)
  skipped          因缺上下文未产出的动作 (见下, 不可省略)
  by_action_type   {action_type: 次数}
  by_zone          {zone_id: 次数}
  by_sensor        {sensor_id: 次数}
  warnings         [{code, message}]
  data_note        数据规模的说明, 显式带上不藏
```

**`skipped` 必须出现在报告里。** 引擎那一层已经规定"条件性上下文运行时为空 →
不产出该 Effect 并记一条, 不静默丢弃"(第二节)。若回放这一层不把它带出来,
对审批人而言它就是被静默丢弃了 —— 引擎守住了边界, 报告又漏出去, 等于没守。

### 警告码

| 码 | 触发条件 |
|---|---|
| `W_HIGH_TRIGGER_RATE` | 折算触发频率超过阈值 (默认每小时 6 次) |
| `W_NEVER_TRIGGERED` | 一次都没**产出** —— 可能条件写太严, 或数据里就没有这种情况 |
| `W_SINGLE_SUBJECT` | 所有触发集中在同一个传感器/区域上 |
| `W_ACTIONS_SKIPPED` | 有动作因缺上下文未产出, message 要说明缺的是哪个字段、多少次 |

**`W_NEVER_TRIGGERED` 的判据是"这条策略一个 Effect 都没产出", 不是"trigger 一次都没命中"。**
两者在正常情况下等价, 但恰恰在最需要说清的那种情况下不等价: 数据缺字段时 trigger
每次都命中、只是动作全被跳过 —— 按"命中"算就不会出这条警告, 而那正是审批人最该看到
一条警告的时刻。

**`W_ACTIONS_SKIPPED` 与 `W_NEVER_TRIGGERED` 必须能同时出现。** 二者一起才说得清
"一次都没产出"的真实原因: 是条件写太严 (前者不出), 还是数据缺字段导致动作全被跳过
(前者会出)。只给 `W_NEVER_TRIGGERED` 会把人引向改条件, 而条件根本没错。

**触发频率的分母必须是实际仿真时长 `span_s + tail_s`, 不是 `span_s`。**
tail 期间的触发照样进分子, 分子分母必须是同一段时间。用 `span_s` 做分母时,
"短事件 + 长尾巴"的场景 (`auto_close.yaml` 正是这种) 会算出荒唐的频率并误报 ——
实测 1 秒跨度 + 3600 秒尾巴、只触发 1 次, 会算成每小时 3600 次。

### 只出警告, 不出拒绝

**这一条是硬性规定, 不是实现者可以自行加强的。**

历史上没误报, 不等于上线后不误报。344 条读数只覆盖 5 个探头、时间跨度有限, 它是一个
**样本, 不是全集**。若做成硬性拦截 (触发超过 N 次就不许发布), 等于**用一个有限样本
替人做了决定**, 这是过度承诺。做成警告则诚实: 数字摆在审批人面前, 判断权在人手里 ——
人工审批那一关本来就是干这个用的。

`ReplayReport` 里必须显式带上数据规模的说明, 不藏。面试口径同此:
"我保证不了策略不误报, 但我能在发布前把它在真实历史数据上的表现量化给审批人看。"

---

## 七、验收

1. **Schema 层**: 白名单外的 type 直接失败; `requires_approval`、`name` 等 DSL 外字段
   被 `extra="forbid"` 拒绝。
2. **语义层**: 每个错误码至少一条测试; 错误码带 path 与 hint, 仅凭错误码即可完成修复循环。
3. **`E_CONTEXT_UNAVAILABLE`**: `device_offline` 触发 + `close_incident` 动作的策略被拦住,
   hint 指出该 trigger 能提供哪些上下文。
4. **计数语义**: 用 `multi_sensor_escalation` 在 t=200s 断言计数为 2 (读法甲),
   这条测试的存在本身就是防止实现漂移成读法乙。
5. **升级链路 (重写原验收 3)**: 场景 `multi_sensor_escalation` + 下面这条示例策略 ——

   ```
   scope:      {type: zone, ids: [1]}
   trigger:    {type: incident_elapsed, in_status: open, for_s: 120}
   conditions: [{type: wet_sensor_count, count_within: same_zone,
                 op: ">=", value: 2, window_s: 180}]
   actions:    [{type: notify, channel: email, target_role: manager},
                {type: set_led, target: incident_device, state: ON}]
   cooldown_s: 600
   ```

   前置: 另有一条"传感器变湿就开事故"的策略, 使 1 号探头 t=5s 变湿后开出事故 1、
   2 号探头 t=40s 变湿后开出事故 2 (事故事件由投影器产生, 见第六节)。
   **这条前置策略的 scope 必须是 `{type: sensor, ids: [1,2]}`**: 两次变湿相隔 35 秒,
   小于 `cooldown_s` 的下限 60 秒, 用 zone 或 global 分桶会把第二次开事故吞掉,
   事故 2 就不存在, 验收 6 的对照无从谈起。

   事故 1 在 t=125s 满足"open 超过 120 秒", **第一个不早于 125s 的 tick 是 t=130s**
   (tick 间隔 10 秒), 在该 tick 产出 `notify` + `set_led` 两个 Effect,
   `subject.incident_id=1`、`subject.zone_id=1`、**`subject.device_id="Arduino1"`**
   (`set_led` 需要它, 由 `incident_opened` 事件带进来)。

   **原验收写的是"引擎在 t≈125s 产出", 这个数字在架构上做不到** —— t=125s 那一刻
   一个事件都没有 (下一个事件是 t=200s 的刷卡), 事件驱动的引擎不会被唤醒。
   这正是必须引入 tick 的原因, 也是原 SPEC 最深的一处坑。

6. **cooldown 分桶 (边沿与冷却是两个键的对照测试)**: 承上, 事故 2 在 t=160s 也满足
   elapsed —— 它是**另一个触发主体** (incident_id=2), 有自己的边沿, 边沿判定不会拦它;
   拦住它的是 cooldown: scope 是 `{type: zone, ids: [1]}` 时两个事故落在同一个冷却桶
   (zone 1), t=130 刚触发过, 600 秒冷却内不再产出。

   把 scope 改成 `{type: sensor, ids: [1,2]}` 后, 冷却按 sensor 分成两桶, **两条都产出**
   (t=130 与 t=160)。同一份数据、同一条规则, 仅因冷却分桶依据不同而结果不同。

   这条对照守的是"冷却确实按作用对象分桶", 而不是按策略名一刀切。

   **但它守不住"边沿与冷却不是同一个键"** —— 这一点在第一段实现后评审时用变异测试
   验过: 把边沿键改成与冷却同一个分桶依据, 上面两条测试**照样绿**。
   原因是这两个事故的触发时刻只差 35 秒, 小于冷却下限 60 秒, 在这个场景里
   "边沿吞掉"与"冷却吞掉"产出的结果完全一样, 分不开。所以必须另加一条:

   **6b. 判别性测试 —— 边沿按触发主体、不按冷却分桶**: 同区两个传感器变湿相隔 200 秒
   (远大于冷却下限), 使两个事故的 elapsed 时刻也相隔 200 秒。scope 用 zone、
   `cooldown_s=60`, 则第二个事故触发时冷却早已过期, **唯一还能吞掉它的就只有边沿**。
   正确实现两条都产出; 把边沿键合进冷却桶则只产出第一条。
   这条测试才是那个不变量的真正守卫, 不能省。
7. **关单链路 (新增场景包 `scenarios/auto_close.yaml`)**: 一条
   `sensor_dry_for(dry_for_s=300)` + `close_incident` 的策略, 在传感器转干后第 300 秒
   之后的第一个 tick 产出 `close_incident` Effect, `subject.incident_id` 正确。
   **场景末尾必须靠 `tail_s` 的 tick 才能走到关单**, 这条同时验证了 tail 机制。
   (Effect 真正落到 `incidents.resolved_by = policy:{id}@v{n}` 是 SPEC-006 的事,
   本 SPEC 只管引擎产出了正确的 Effect。)
8. **回放报告**: 拿 344 条真实 CSV (`apps/device-sim/seed/waterlevel_readings.csv`)
   跑一条示例策略, 输出完整 `ReplayReport`; 触发过密时给出 `W_HIGH_TRIGGER_RATE`
   且**不阻断**。CSV 的装载由测试调用 `packages/scenario` 的 loader 完成
   (那个包允许读文件), 回放模块本身仍不碰文件系统。

   **这份 CSV 曾有的限制, 已在第二段解除**: `events_from_csv` 产出的事件没有 `zone_id`
   (那一列本来就不在遥测报文里, zone 的事实源是数据库), 一度导致任何需要 zone 上下文的
   动作 (`open_incident`) 在 CSV 回放上产出恒为 0、全部落进 `skipped`。
   第二段的 `policy_runtime` 在事件规范化时从 `sensors` 表补上了 `zone_id`,
   **CSV 现在对全部动作类型可用**。

   第三段走 HTTP 的 `simulate` 接口实测 (作用域 zone [1,2,3]、`cooldown_s=60` 的
   "变湿就开事故"策略): **产出 65 次 `open_incident`, `skipped` 为 0**;
   按区 3 / 2 / 1 分别 33 / 23 / 9 次, 按传感器 0 / 1 / 2 / 4 / 5 分别
   9 / 8 / 1 / 23 / 24 次。`events_count` 是 1258 而不是 344 ——
   装载器按仿真时间补了心跳事件, 真实读数仍是 344 条。

   **报出这类数字时必须带上产生它的策略配置**: 同一份数据换个作用域, 结果差得很远
   (全局分桶只有 35 次, 按传感器分桶 68 次) —— 不带配置的数字没法复现, 也就没法核对。

   报告仍应靠 `skipped` + `W_ACTIONS_SKIPPED` 把"动作被跳过"这件事说出来,
   而不是只给一条 `W_NEVER_TRIGGERED` 让人以为是条件写太严。

8b. **触发频率的分母**: 用 `span_s + tail_s` 而不是 `span_s`。
   配一条回归测试: 短跨度 + 长 tail、只触发一次时, **不得**报 `W_HIGH_TRIGGER_RATE`。
9. **纯函数边界**: `packages/policy_engine` 的零 IO 断言测试继续通过
   (AST 扫描包内 import, 与 `packages/scenario` 的边界测试是两份, 标准更严)。
10. **现有场景文件的注释要跟着改**: `scenarios/multi_sensor_escalation.yaml` 头部注释
    现在写着"notify(**zone_manager**)"与"unacknowledged 条件在 t≈**125s** 满足",
    两处都已被本次定稿推翻 (角色枚举里没有 `zone_manager`, 正确时刻是 t=130s)。
    **只改注释行, 不得改动任何 `events`** —— 那些事件被别的测试依赖着。
    留一段与定稿 SPEC 相反的注释, 正是本项目最忌讳的"两份走散"。
11. 测试命名遵循 `test_<行为>__<条件>`; `make lint` 与全部测试绿。

## 不在本 SPEC 范围

版本化、审批、发布、引擎接管开关事故、tick 后台任务、Automation Studio 后端接口:
**SPEC-006**。Agent 编排状态机: SPEC-002 (W4)。评测集与消融: W5。

---

## 评审记录

**2026-08-07, W3 开工前逐条评审, 整篇重写。**

原 SPEC-001 是 W1 之前的草稿 (673 字节), 从未按 W1/W2 的标准过审。对照 SPEC-003 (9 KB)、
SPEC-004 (10.5 KB) 的体量就能看出差距 —— 它连"接口"一节都没有, 而且把语言定义外包给了
代码 ("白名单见 dsl.py"), 代码的 docstring 又指回 SPEC, 循环引用。
`CLAUDE.md` 的流程是"功能先写 SPEC", 原状态是代码先有、SPEC 反过来引用代码,
与 W1 的 ruff 红灯、W2 的 mypy 空转是同一类毛病: **声明的和执行的不是一回事**。

评审确认的问题, 按严重程度:

**架构层 (不解决则 W3 开不了工)**

1. **DSL 里没有开事故的动作**, 而 W3 的任务就是让引擎接管开事故 → 新增 `open_incident`。
2. **引擎声称零 IO, 但一半条件需要数据库里的事实** → 事故状态改由事件流进入引擎,
   "事件流是引擎唯一输入"升格为显式不变量。
3. **纯事件驱动表达不了"过了多久还没发生事"**, 而原验收 3 恰恰要求它做到 → 引入 tick,
   并为模拟器补 `tail_s`。
4. **Effect 不带作用对象**, 执行器不知道点哪台灯、升哪条事故 → `subject` 强制化。

**定义层 (不写死则实现者必然猜错)**

5. 作用域缺失且与条件内字段撞词 → 顶层 `scope`, `zone_in` 删除, 条件内字段改名。
6. cooldown 按策略名分桶 → 改按 `(policy_id, 作用对象)`, 并写明冷却抑制产出而非跳过判断。
7. `wet_sensor_count` 有两种读法且代码里两种暗示都有 → 定为读法甲并配对照测试。
8. 角色名三套互不相干, 导致 `E_ROLE_NOT_STAFFED` 对每条通知策略都误报 → 统一到 `roles` 表。
9. 审批分级无任何用途 → 定为界面提示用途, 并补键集合一致性断言。
10. 动态验证在原 SPEC 里一个字都没有 → 补第六节, 并写死"只出警告不出拒绝"。
11. 策略名字在表和 DSL 里各存一份 → DSL 里删除。

**2026-08-07 第一段实现后, 逐行复核代码时发现的五处 (已改入正文)**

代码本身质量高, 50 条测试全绿, 报告与实现一致。问题出在 SPEC 自己和测试的守卫强度上:

15. **回放报告漏掉了 `skipped`**。引擎老实记了 (真实 CSV 上 35 条), 但报告没有字段装它,
    到审批人眼前就是静默丢弃 —— 引擎守住的边界在报告这一层又漏出去。
    已补 `skipped` 字段与 `W_ACTIONS_SKIPPED` 警告码。
16. **触发频率的分母算错**: 分母只算遥测跨度、分子却含 tail 期间的触发。
    实测 1 秒跨度 + 3600 秒尾巴、只触发 1 次 → 算成每小时 3600 次, 直接误报。
    `auto_close.yaml` 这类"短事件 + 长尾巴"的场景必然中招。已写死分母为 `span_s + tail_s`。
17. **验收 6 的对照测试守不住它声称守的东西**。用变异测试验过: 把边沿键合进冷却桶,
    那两条**照样绿**。已补判别性的验收 6b。
18. **验收 8 建立在错误假设上**: 那份 CSV 没有 `zone_id`, 开事故类策略在它上面恒为 0。
    已写明限制与补救归属。
19. **`incident_elapsed` 的起算点写错**: 一律从开单时刻起算, 会让
    `in_status=acknowledged` 的语义与字面相反。已改为从进入该状态起算。

另补三处 SPEC 原本没写死、由实现者定的口径 (均采纳实现的选择):
`wet_sensor_count` 的窗口不锚定; 命中即计冷却哪怕动作全被跳过; 传感器首次上报即视为
一次状态变化。

**范围调整**

12. `time_window` 砍到 v2 (无时区、跨午夜未定义、破坏可复现性)。
13. `operator_on_duty` 砍到 v2 (系统内无排班数据, 是空承诺)。
14. `close_incident` 本次**加入** —— 原计划留在 service 层, 评审时决定一并搬进 DSL:
    规则集中、可分区配置、模拟器能验证关单、改关单标准也要走审批。
