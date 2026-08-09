# SPEC-006 · W3 策略的版本、审批、发布与引擎接管

状态: **已实现 (W3, 2026-08-07)**。分三段落地: 引擎与验证器 / 数据库与 service 层 /
HTTP 接口, 每段实现后逐行复核并做变异测试, 记录见 `docs/进度与交接.md` 的 W3 章节。

**2026-08-08 W4 开工前评审补三处** (原文其余部分未动, 三处均标了"W4 补充"):
第二节 status 取值加 `discarded`; 第二节"版本不可变"划出边界 (不可变从提交审批开始);
第三节补上 `approvals.task_id` 的填写入口与 `agent_tasks` 的回写。依据见 SPEC-002。

策略语言本身与两层验证器见 **SPEC-001**。本 SPEC 覆盖围绕这门语言的一切:
草稿怎么变成生效的规则、谁批准、怎么发布、引擎怎么接管开关事故、
Automation Studio 需要哪些后端接口。

**这份文档是 `CLAUDE.md` 不变量 1 的唯一落地点。**
"模型永不直接执行副作用, 必须人工审批"是整个项目的核心主张,
它成不成立, 全看这份 SPEC 里的东西做没做扎实。

## 目标

一条策略从"模型产出的草稿"走到"线上正在跑的规则", 全程有版本、有审批、有留痕、可回滚,
且**"没有审批就不能发布"这件事由数据库强制, 不靠应用层自觉**。

---

## 一、最要紧的一件事: "DB 层校验"目前是空的

`CLAUDE.md` 不变量 1 写的是"publish 必须存在 approvals 记录 (**DB 层校验**)"。

实际情况: `policy_versions` 有一列 `status`, 取值里有 `published`。所谓发布就是把这一列
改成 `'published'`。而 `approvals` 表与它之间**没有任何强制关联** —— 任何一条 SQL 都能
把 status 直接改成 published, 数据库不会拦。

所以真正在拦的只有应用层代码里的一个判断。而应用层代码可以被绕过, 也可能在某次重构里
被人顺手删掉 —— **删掉之后没有任何测试会红**。

这与 W1 的 ruff 红灯、W2 的 mypy 空转是同一类毛病 (**声明的和执行的不是一回事**),
只是这次踩在项目最核心的主张上。

### 解决: 把"发布"变成一张表, 审批 id 设成 NOT NULL 外键

```sql
CREATE TABLE policy_publications (
    id                bigserial PRIMARY KEY,
    policy_id         bigint NOT NULL REFERENCES policies(id),
    policy_version_id bigint NOT NULL REFERENCES policy_versions(id),
    approval_id       bigint NOT NULL REFERENCES approvals(id),   -- 关键
    published_by      bigint NOT NULL REFERENCES users(id),
    published_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at        timestamptz,
    revoked_by        bigint REFERENCES users(id)
);

-- 每条策略最多只有一个生效版本
CREATE UNIQUE INDEX policy_publications_one_active
    ON policy_publications (policy_id) WHERE revoked_at IS NULL;
```

`approval_id` 是 **NOT NULL 的外键**: 想往这张表插一行 (也就是"发布"), 必须给出一个
真实存在的 approval id。**没有审批记录, 这一行在物理上插不进去** ——
数据库直接拒绝, 与应用代码写成什么样毫无关系。

那条 partial unique index 让"现在生效的是哪一版"有唯一答案, 不会出现两版同时挂着
published 的情况 (这个缺口现在真实存在)。手法与 W2 给事故去重时用的是同一个。

这是本项目"约束下沉到数据库"的第三次应用:

1. W1 —— 幂等唯一键, 重放不产生重复行;
2. W2 —— 事故 partial unique index, 同一传感器不会开出两个未解决事故;
3. W3 —— 发布的审批外键, 没批过就发不出去。

一句话: **凡是必须永远成立的规则, 由数据库保证, 不靠代码自觉。**
为什么选外键而不是数据库触发器, 见 `docs/adr/ADR-007`。

---

## 二、状态与两张表的分工

**`policy_versions.status` 与 `policy_publications` 不冗余, 各答各的问题:**

- `status` 回答"**这一版加工到哪一步了**": draft → validated → simulated →
  awaiting_approval → published / rejected / discarded
- `policy_publications` 回答"**现在线上跑的是哪一版**"

(`discarded` 是 **W4 迁移 `0008` 新增**的取值: Agent 编译失败留下的半成品草稿标成它,
列表接口默认过滤掉, Trace UI 与 W5 评测仍读得到。**不能复用 `rejected`** —— 那个词在
本 SPEC 里已经是"人审批时否决"的意思, 一词两义之后 W5 就没法把"人看了觉得不行"与
"模型自己没编译出来"分开统计, 而这两类是完全不同的样本。见 SPEC-002 第六节。)

一个版本可以是 `published` (审批通过过) 但已被更新的版本取代、当前并不生效。
两个问题不同, 所以两处并存不是走散风险 —— 这与 SPEC-001 里"策略名字不能存两份"
是同一套判断标准: **同一个事实存两份才叫冗余, 两个不同的事实分开存是正常建模。**

`0001` 建表时 status 的 CHECK 里有 `rolled_back` 而没有 `awaiting_approval`,
迁移 `0007` 里调整: 去掉 `rolled_back` (回滚是撤销发布记录, 不改版本状态),
加入 `awaiting_approval`。

### 状态流转

```
              validate            simulate          request-approval
  draft ───────────────► validated ────────► simulated ──────────────► awaiting_approval
    ▲                                                                     │
    │  改草稿即新建一版                                      decide(approved) │  decide(rejected)
    │                                                                     ▼         ▼
    └──────────────────────────────────────────────────────────────── published  rejected
                                                                          │
                                                                    publish │ (写 policy_publications)
                                                                          ▼
                                                                      线上生效
```

- **版本不可变**。改一个字就是新建一版, 已有版本永不原地修改 ——
  这样"当时批的到底是什么"永远查得到, 与 SPEC-003 "resolved 是终态、重开就开新事故"
  是同一个思路: 事实不被改写。

  **不可变的边界是"提交审批" (W4 补充)。** 一旦进入 `awaiting_approval`, 这一版永远
  不再改动 —— 从这一刻起有人要看它、批它, "当时批的到底是什么"必须永远查得到。
  在那之前的 `draft` 是**工作台**: W4 的 Agent 修复循环就在同一行草稿上就地改 body,
  不每修一次就新开一版 (中间态原样记进 `agent_steps`, 一条不丢, 见 SPEC-002 第六节)。

  这条边界不是放宽, 是把原来那句话说准了: 不可变保护的从来不是"所有写过的字",
  而是**"已经被人看过、成为过决策依据的东西"**。Agent 改到一半的草稿谁也没看过,
  也没进入过审批, 不存在"当时批的是什么"这个问题。
  (在**已有策略**上改则必须先新开一版 —— 那些版本可能已经批过、发过, 动不得。)
- 推进一律用条件更新 `UPDATE ... WHERE status = 期望的旧状态`, 受影响行数为 0 即 **409**,
  沿用 SPEC-003 的做法。
- **`published` 与"生效"是两件事**: 批准通过让版本进入 published, 但线上跑不跑由
  `policy_publications` 决定。批准的是"这一版可以上线", 什么时候上线是运营动作。

---

## 三、审批

### 表结构补充 (迁移 0007)

`approvals` 表 `0001` 已建 (`task_id` / `policy_version_id` / `requested_by` /
`decided_by` / `decision` / `decided_at`), 补四样 (两列、一个索引、一条 CHECK):

```sql
ALTER TABLE approvals ADD COLUMN requested_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE approvals ADD COLUMN note text;

-- 一个版本同时最多一条待决审批
CREATE UNIQUE INDEX approvals_one_pending
    ON approvals (policy_version_id) WHERE decision IS NULL;

-- 不得自己批自己
ALTER TABLE approvals ADD CONSTRAINT approvals_no_self_approve
    CHECK (decided_by IS NULL OR decided_by <> requested_by);
```

- **"待审批"用 `decision IS NULL` 表达**, 不新增状态列。够用, 且那条 partial unique
  index 正好挂在这个条件上。
- `task_id` 允许为空: W3 的策略由人直接在 Studio 里写, 没有 Agent 任务;
  W4 Agent 产出的草稿才会填。

  **W4 补上了填它的入口** (W3 实现时 `request_approval` 没有这个参数, 这一列一直没人
  写得进去): `policy_service.request_approval` 加一个 `task_id` 参数 (默认 None);
  对称地, `policy_service.decide_approval` 在决定做完之后, 若这条审批挂着 `task_id`,
  把对应的 `agent_tasks` 推进到 `completed`。**批准与否决都推进** —— 批没批通过与
  Agent 无关, 它的活在提交审批那一刻就干完了 (SPEC-002 第四节)。

### 禁止自己批自己 —— 数据库与应用层各做一层

**两层都做, 分工明确:**

- **数据库层的 CHECK 是兜底不变量**。"必须人工审批"这条主张, 如果允许提交人自己批,
  就等于没有审批。这是一条永远不该有例外的规则, 所以放数据库。成本只有一行。
- **应用层先返回 403 并写审计**, 因为数据库约束被违反时抛出来的是一个
  IntegrityError, 对用户没有任何意义。应用层负责给出可读的错误信息。

(评审时先只打算做应用层, 定稿改为两层 —— DB 那行 CHECK 太便宜, 而这条恰恰是不变量。
判断标准写在 ADR-007: **不变量下沉到数据库, 可能有例外的业务规则留在应用层**。
跨区派单需要显式放行、属于"有例外"那一类, 所以它留在应用层, 与本条对照。)

### 种子要加第二个 manager

现有种子三个账号: `admin@example.com` (admin)、`chris@example.com` (manager, 绑 Chris Li)、
另一个 operator。

**为什么必须有第二个 manager**: 不是因为"提交人和审批人必须是两个人" —— operator 提交、
manager 批准这条主线, 一个 manager 就够。真正的原因是 **manager 自己也会写策略**,
而他写的策略按不变量必须由**另一个** manager 批。只有一个 manager 时, 这条路径直接死锁:
他提交的任何策略永远发不出去。这不是演示方便与否的问题, 是系统在单 manager 部署下
不可用。

种子新增一个 manager 账号 (例: `dana@example.com`)。顺带也让"自批被拦住"这条验收
可以真实走一遍: manager A 提交 → manager A 自己批 → 403 → manager B 批 → 通过。

顺带补 SPEC-005 遗留项里提到的 **viewer 账号**: 现在"viewer 看不到操作按钮"只在后端
测试里验过 (夹具自己插的), 网页上从没真实点验。一并加进种子。

### "第二个人"在现实里是谁 (W4 补记, 2026-08-08)

W4 真人点界面时提了一个好问题: **一家超市配两个店长, 现实吗?** 不现实。
这条规则的框架在上面那一段里被写窄了, 补正如下。

**规则本身不是我们编的**: 它是变更管理里的职责分离 (有的地方叫"四眼原则"),
在支付、运维、金融里都是硬要求。适用它的理由不是"店里有几个人", 而是
**改的不是货架摆位, 是一条会自动开工单、自动发邮件的生产规则** ——
改它等于改生产配置, 而生产变更要有第二双眼睛。

**真正没写清的是"第二个人是谁"**: 现实部署里那个人**不是同一家店的第二个店长**,
而是区域经理、总部运营, 或连锁体系里的 IT/运维负责人 —— 在层级上更高,
或者在职能上属于另一条线。种子里放两个 manager 只是**让这条路径在演示环境里跑得通**,
那是测试数据的权宜, 不是对现实组织的建模。这一句要写进 README 与面试口径,
否则读的人会以为我们真的认为每家店有两个店长。

**一刀切也是过头的, 但 v1 刻意先一刀切。** 更贴近现实的做法是按风险分级:
会往外发邮件、影响全店的动作需要第二个人; 只把后场的冷却时间从 300 改成 600,
自己批 + 留痕就够。

**钩子已经在代码里**: SPEC-001 的 `ACTION_APPROVAL_CLASS` 把动作分成
`external_side_effect` 与 `internal_write`, v1 刻意只让它做审批界面的风险提示、
**不改变审批门槛**。v2 的自然一步就是让这个分级决定"要不要第二个人"。

**v1 不放宽的理由**: "不得自己批自己"目前是数据库 CHECK 约束 (本节上方),
放宽它要动迁移与那条约束本身。先把不变量立住、再按分级放宽, 顺序不能反 ——
反过来做的话, 中间那段时间"必须人工审批"这条主张是空的。

---

## 四、引擎接管开事故与关事故

### 要删掉的东西

`services/incident_service.py` 里 `apply_sensor_state()` 这个函数**整体删除**,
它是 W2 就写明的临时占位 (SPEC-003 决策 1)。删的是**判断逻辑**:

- 转湿即开事故 (`_OPEN` 那段 SQL 与调用);
- 转干后按 `auto_resolve_dry_seconds` 稳定窗口自动关单 (`_LAST_WET_TS` / `_AUTO_RESOLVE`)。

**不做兼容层, 不留开关。** 留着"策略引擎没配规则时回退到硬编码"这种后路, 就会出现
"线上到底按哪套规则在跑"说不清的状态, 而这正是整个项目要修的毛病。

配置项 `SENTINEL_AUTO_RESOLVE_DRY_SECONDS` 一并删除 —— 它的职责被
`sensor_dry_for` 触发器的参数取代, 而且现在可以按区配不同的值。

### 不删的: 两类遥测时间线记录

`sensor_still_wet` 与 `sensor_dry` 这两类 `incident_events` 记录**必须保留**。
它们不是判断逻辑, 是**事实记录** —— SPEC-003 决策 2、决策 4 与两条验收都依赖它们
("同一传感器持续报湿 → 不新增事故, 时间线累加 `sensor_still_wet`"、
"每次转干仍记一条 `sensor_dry`, 无论是否达到自动解决条件")。
跟着判断逻辑一起删掉, SPEC-003 的验收会当场变红, 而且事故时间线上会出现
"开了之后什么都没有直到被关掉"的空白段。

做法: 从 `apply_sensor_state()` 里拆出一个只记时间线、不做任何判断的函数
(例: `record_sensor_observation(session, sensor_id, wet, ts)`), 由 `/ingest` 在
sensorstate 被推进后调用。它查该传感器有没有未解决事故, 有就追加一条时间线, 没有就返回。
**这个函数不开单、不关单、不改任何状态**, 与策略引擎的职责界限一目了然。

### 新增的东西

**`services/policy_service.py`** —— 唯一能碰策略相关表的层 (`CLAUDE.md` 不变量 4)。
草稿、版本、校验、模拟、审批、发布、撤销全部在这里, Agent tools 与 W6 的 MCP server
复用同一份, 不另写一套。

**`services/policy_runtime.py`** —— 把引擎接进请求链路:

1. 加载当前生效的策略集 (`policy_publications` 里 `revoked_at IS NULL` 的那些版本),
   **带进程内缓存**, 发布/撤销时失效;
2. 把 `/ingest` 的事件与事故域事件规范化成引擎的 `Event`;
3. 调 `evaluate()` 拿到 Effect 序列;
4. 交给 Effect 应用器。

**Effect 应用器** —— 唯一产生副作用的地方:

| action_type | 落到哪 | 幂等保证 |
|---|---|---|
| `open_incident` | `incident_service.open_incident()` | 撞上 partial unique index 即空操作 |
| `close_incident` | `incident_service.close_incident()` | 事故已 resolved 即空操作 |
| `escalate_incident` | `incident_service` 更新 severity | 目标等级已达到即空操作 |
| `notify` | W3 只写 `policy_runs`, **不真发邮件** | — |
| `set_led` | W3 只写 `policy_runs`, **不真下发** | — |

`notify` / `set_led` 的落点要看有没有事故: **`subject.incident_id` 有值时**额外在
`incident_events` 上记一条 (让事故时间线看得见"系统在这一刻决定通知谁");
**没有值时只落 `policy_runs`** —— `incident_events.incident_id` 是指向 `incidents`
的外键, "设备离线就通知管理员"这类 Effect 没有事故可挂, 硬塞会撞外键。

**通知与点灯 W3 不接真实出口**, 理由: 原系统的 SES 与 IoT Core 已随小组账号注销,
W3 接一个假的外发通道只会掩盖问题。落 `policy_runs` 与事故时间线, Dashboard 能看见
"策略在这一刻决定要通知谁", 演示与评测都够用。真实出口在 W6 上线时补, 记在 ADR。
**这一条必须写在响应体与文档里, 不能让人误以为邮件真的发出去了。**

### 事故域事件的投递

`incident_service` 在推进状态的**同一个事务**里, 除了现有的 `incident_events` +
`audit_log`, 再向 `policy_runtime` 投递一条域事件 (`incident_opened` /
`incident_assigned` / `incident_acknowledged` / `incident_resolved`)。

事务回滚则事件不存在, 引擎看到的与数据库里的**永远一致**。这是 SPEC-001 第一节那条
"事件流是引擎唯一输入"能成立的前提。

**要防的一件事: 递归。** `open_incident` 这个 Effect 会让 `incident_service` 产生一条
`incident_opened` 事件, 该事件又会进引擎。实现上必须是**先收集完本轮全部 Effect、
应用完, 再把新产生的域事件排进下一轮**, 而不是在应用 Effect 的过程中递归调用引擎。
本轮内产生的域事件在**下一个 tick** 才被消费。
SPEC-001 的 `E_SELF_TRIGGER_LOOP` 静态检查是第一道防线, 这条是第二道。

### 对 SPEC-003 的修订 (本 SPEC 负责收口)

引擎接管开关事故后, SPEC-003 有三处口径要跟着改。这里写明, **实现时一并改掉
SPEC-003 的对应段落**, 不留新旧两套说法并存。

1. **`incidents.resolved_by` 的自动解决取值**: 从 `'auto_sensor_dry'` 改成
   **`'policy:{policy_id}@v{version}'`**。信息量变多 —— 能追到是哪条策略的哪一版关的。
   SPEC-003 决策 4 "把人处理的与自己好了的分开统计"这个用途不受影响:
   判据从"等于 auto_sensor_dry"改成"以 `policy:` 开头", 而人工解决仍以 `employee:`
   或 `user:` 开头。需要同步修改的位置: SPEC-003 的状态机图、决策 4、验收清单里
   写着 `auto_sensor_dry` 的那几行, 以及 SPEC-004 决策里列 actor 口径的那一句。
2. **决策 1 标记为已兑现**: "W2 先用硬编码规则, W3 由 Policy 引擎接管, 届时整体删除" ——
   加一句"已于 W3 兑现, 见 SPEC-006 第四节"。
3. **稳定窗口的归属**: SPEC-003 决策 4 里"要求连续 `SENTINEL_AUTO_RESOLVE_DRY_SECONDS`
   秒没再报湿"这条, 改为指向 `sensor_dry_for` 触发器的 `dry_for_s` 参数,
   并注明现在可以按区配不同的值。

已知边界那条 ("最后报干后彻底静默的传感器不会被自动关单") **仍然成立且原因不变**:
`sensor_dry_for` 由 tick 驱动, 反而比原来更宽松 —— 原实现要等下一条干燥事件到达才判断,
现在 tick 会主动检查。这算一处顺带的改善, 值得在 SPEC-003 那条边界旁边补一句。

### 引擎状态的推进与副作用必须同生共死

**只要一轮 evaluate 的副作用没能落库, 这一轮推进的引擎状态就必须整体回退。**
做法: 进引擎前对状态做快照, 事务失败时还原, 并把本轮取走的域事件放回队首,
下一轮原样重试。

**这条对 tick 与遥测两条路径同等适用, 不能只保护其中一条。** 理由是同一个:
引擎的边沿触发与冷却都是"一次性"的 —— 状态推进了而副作用没落库, 那条 Effect
就**永远补不回来**。

遥测路径尤其致命, 因为 `sensor_state_changed` 是**边沿**触发: 传感器持续报湿
不构成新边沿。所以一次数据库抖动之后, 那张本该开出来的工单要等到传感器
先转干、再转湿才有机会补开 —— 对一个"别漏掉漏水"的系统, 这是最难看的失败:
告警静悄悄没了, 日志里只有一条 500, 没人知道有张单被吞了。

(第二段实现时只保护了 tick 一侧, 遥测一侧漏了, 复核时实测确认: 第一次应用失败后
`wet_since` 与 `last_fired` 都已推进, 同一传感器再报湿时 `open_incident` 调用零次。)

**这两条保护都必须各配一条注入失败事务的测试。** 只实现不测等于没做 ——
下一个人觉得"快照太重"顺手删掉, CI 不会拦。第二段就是这个状态: tick 那侧的回退
写了, 但 8 条 runtime 测试没有一条注入失败, 删掉那行照样全绿。

### tick 后台任务

- `apps/api` 启动时拉起一个 asyncio 后台任务, 每 `SENTINEL_ENGINE_TICK_SECONDS`
  (默认 10) 秒投一个 `tick` 事件。
- **不进 `agent_tasks` 表**, 那张表有审计/重试/死信语义; tick 是纯时钟, 与 SPEC-005
  把演练状态放内存是同一个判断。
- **已知边界: 多实例部署会重复 tick。** W3 是单实例, 记在这里;
  W6 若扩多实例需要选主或改用数据库咨询锁。这一条要写进代码注释, 不能只在文档里。
- 关停时要能干净取消, 否则测试会挂住。

---

## 五、Automation Studio 后端接口

前端在 W4。W3 把接口按 W4 需要的形状做好, 省一次返工。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/policies` | viewer+ | 列表, 含当前生效版本号与状态 |
| POST | `/policies` | operator+ | 新建策略 + 第一版草稿 `{name, body}` |
| GET | `/policies/{id}` | viewer+ | 单条 + 全部版本 + 当前发布记录 |
| POST | `/policies/{id}/versions` | operator+ | 新增草稿版本 `{body}` |
| GET | `/policy-versions/{id}` | viewer+ | 单版本详情 |
| POST | `/policy-versions/{id}/validate` | operator+ | 静态校验 → `issues[]`, 通过则转 validated |
| POST | `/policy-versions/{id}/simulate` | operator+ | 动态回放 `{source}` → `ReplayReport`, 跑完即转 simulated |
| POST | `/policy-versions/{id}/request-approval` | operator+ | 建 approvals 行, 版本转 awaiting_approval |
| POST | `/approvals/{id}/decide` | **manager+** | `{decision, note}`; 自批返回 403 |
| POST | `/policy-versions/{id}/publish` | **manager+** | 要求已批准; 写 `policy_publications` |
| POST | `/policies/{id}/revoke` | **manager+** | 撤销当前发布 |
| GET | `/policy-runs` | viewer+ | 线上触发历史, 可按策略/时间过滤 |
| GET | `/employees` | viewer+ | **补 W2 遗留项**, 派单下拉框要用 |

几处刻意的选择:

- **发布是独立一步, 不与审批合并。** 批准的是"这一版可以上线", 什么时候上线是运营动作。
  分开之后 publish 那一步的外键校验才有独立意义。
- **publish 与 revoke 都收归 manager**, 与 SPEC-004 决策 6 权限表那一行
  ("W3: 审批发布 policy —— manager 以上") 逐字一致, 不对既有权限表做任何修订。
  曾考虑把 publish 放宽到 operator (审批已完成, 再要求 manager 点一次不增加安全性),
  但那需要改 SPEC-004 的权限表, 而**跨文档的权限口径分叉正是本项目反复在修的毛病**,
  不值得为省一次点击付这个代价。
- 演示流程因此是: operator 提交 → manager A 批准 → manager A 发布;
  manager 自己写的策略则是 manager A 提交 → manager B 批准 → 任一 manager 发布。
- **`GET /employees` 顺手补上**: Dashboard 的派单现在是手填员工 ID 数字框, 前端硬编码
  员工名单等于重蹈 `sensorZoneMap` 的覆辙。权限与 `/incidents` 同档。
- **simulate 是"跑完即转", 不是"通过才转"**。回放按 SPEC-001 第六节的硬性规定
  只出警告不出拒绝, 所以不存在"没通过"这种状态 —— 只有"跑出异常"(那是 500, 不转移)
  和"跑完了"(转 simulated, 警告原样带回给审批人看)。措辞上不要写成"校验通过",
  那会诱导实现者加一道 SPEC 明令不要的拦截。

### 发布前的跨策略检查

`publish` 时除了外键, 还要做两项**需要读到其它已发布策略**的检查
(超出纯函数边界, 所以放在 service 层而不是 `packages/policy_engine` 里):

- **动作互斥**: 同一 scope 内已有策略对同一对象产生相反动作 (如一条 `set_led ON`
  一条 `set_led OFF`) → 拒绝发布并指出是哪条策略。
- **跨策略自触发环**: A 的动作能唤醒 B、B 的动作能唤醒 A → 拒绝发布。

这两项是**拒绝**而不是警告, 与动态验证的"只出警告"不同 —— 区别在于:
跨策略冲突是**当下就能确定的事实**, 动态验证的误报率是**从有限样本外推的推测**。
确定的事实可以拦, 推测只能提示。

### 回滚

撤销当前发布 + 重新发布旧版本。**旧版本复用它当年那条审批记录, 不需要重新审批** ——
那一版本来就批过, 而且版本不可变, 批的是什么现在还是什么。
这是"版本不可变"这个设计带来的直接好处, 值得在文档和面试里点一句。

---

## 六、数据库变更 (迁移 0007)

一个迁移, 手写, 必须写 downgrade (ADR-006)。内容:

1. 新建 `policy_publications` 表 + 那条 partial unique index;
2. `approvals` 加 `requested_at` / `note`, 加 `approvals_one_pending` index,
   加 `approvals_no_self_approve` CHECK;
3. `policy_versions.status` 的 CHECK 调整: 去 `rolled_back`, 加 `awaiting_approval`;
4. `policy_runs` 加 `policy_id` 冗余列与时间索引 (按策略查触发历史是最常见的查询);
5. **删除 `policies.enabled` 这一列**。它是 `0001` 建表时留的布尔开关, 表达的也是
   "这条策略生不生效" —— 与 `policy_publications` 撞了同一个事实。按本 SPEC 第二节
   自己立的标准 ("同一个事实存两份才叫冗余"), 这一列必须收口。留着它, 迟早会出现
   `enabled=false` 但 `policy_publications` 里有生效记录的状态, 而**没有任何东西会报错**;
6. 种子: 第二个 manager 账号 + 一个 viewer 账号
   (viewer 是 SPEC-005 的遗留项 —— "viewer 看不到操作按钮"至今只在后端测试的
   夹具里验过, 网页上没真实点验)。

`SENTINEL_AUTO_RESOLVE_DRY_SECONDS` 配置项删除 (无迁移, 改 `config.py`)。

**新模块必须进 mypy 严格档白名单** (`mypy.ini`), 否则新代码会悄悄退回默认档 ——
这正是 ADR-005 要防的"声明与执行不一致"。涉及 `app.services.policy_service`、
`app.services.policy_runtime`、`app.routers.policies` 等。

---

## 七、验收

**主线 A —— operator 提交, manager 批准** (交接文档里写的那条):

1. 用 operator 账号写一条策略 → 草稿建立;
2. **静态校验拦住一个写错的引用** (例: scope 指向不存在的 zone 99) → 返回
   `E_UNKNOWN_ZONE` 且 hint 给出合法取值;
3. 改对之后校验通过 → 版本转 validated;
4. **拿 344 条真实历史数据回放** → 返回 `ReplayReport`, 含触发次数与分布,
   触发过密时给 `W_HIGH_TRIGGER_RATE` 但**不阻断**, 版本照常转 simulated;
5. 提交审批 → 版本转 awaiting_approval, `approvals` 出现一条 `decision IS NULL` 的记录;
6. **operator 尝试自己批 → 403**。注意这一步证明的是 **RBAC** (operator 无权审批),
   不是"禁止自批" —— 两者验的不是同一件事, 不要混为一谈;
7. manager A 批准 → `decision='approved'`, 版本转 published;
8. manager A 发布 → `policy_publications` 出现一行, `approval_id` 指向第 7 步那条;
9. **触发场景验证新策略确实接管了开事故** → 跑 `multi_sensor_escalation`,
   `/incidents` 出现事故, 且 `policy_runs` 里能查到是哪条策略哪一版开的。

**主线 B —— manager 自己写的策略 (这条才验"禁止自批")**:

10. manager A 写一条策略并提交审批;
11. **manager A 自己批 → 403 且审计留痕**。他有审批权限 (RBAC 放行), 被拦住的原因
    只能是自批规则 —— 这才是不变量 1 那一半的真实证明;
12. manager B 批准 → 通过, 后续发布正常。

**删干净的证明**:

13. grep 断言 `apply_sensor_state` 与 `auto_resolve_dry_seconds` 在
    **代码与测试中**零残留 (手法同 SPEC-004 的 `X-Actor` 零残留断言, 范围口径照抄它)。
    **不能写成"仓库里零残留"** —— 这两个字符串在 SPEC-003、SPEC-006 的文档正文里
    本来就有, 那样断言必然失败。
14. `sensor_still_wet` / `sensor_dry` 两类时间线**仍然存在**, SPEC-003 的对应验收
    继续通过 (证明删的是判断逻辑而不是事实记录)。

**数据库层的证明** (这几条比上面的端到端更值钱, 因为它们证明的是不变量 ——
端到端只证明"正常路径走得通", 这几条证明"异常路径走不通"):

15. **绕过应用层直接插 `policy_publications` 且 `approval_id` 为 NULL → 数据库拒绝**;
16. `approval_id` 指向一条不存在的审批 → 数据库拒绝;
17. 同一策略插第二条 `revoked_at IS NULL` 的发布记录 → 唯一索引拒绝;
18. **直接用 SQL 插一条 `decided_by = requested_by` 的审批 → CHECK 拒绝**
    (与验收 11 是两层: 那条验应用层的 403 与提示语, 这条验数据库兜底);
19. 同一版本插第二条待决审批 → 唯一索引拒绝;
20. 迁移 `0007` 降一步再升回, 结构与数据一致 (ADR-006 的既定做法)。

其它:

21. 撤销发布后引擎缓存立即失效, 下一个事件不再命中该策略;
22. 回滚到旧版本复用原审批记录, 不产生新的 approvals 行;
23. tick 后台任务在应用关停时干净取消, 测试不挂住;
24. 测试命名遵循 `test_<行为>__<条件>`; `make lint` (ruff + mypy) 与全部测试绿。

## 不在本 SPEC 范围

Automation Studio 前端 (W4, SPEC-005 延续)、Agent 编排 (SPEC-002, W4)、
评测集与消融 (W5)、真实邮件与 LED 出口 (W6)。

## 已知边界 (写进代码注释, 不只写在这里)

- tick 后台任务在多实例部署下重复触发, W3 单实例;
- 生效策略集的进程内缓存在多实例下各存一份, 发布后失效时机不同步;
- `notify` / `set_led` 在 W3 不产生真实外部动作, 只留记录;
- 动态验证的历史数据只覆盖 5 个传感器、时间跨度有限, 结论是提示性的。
