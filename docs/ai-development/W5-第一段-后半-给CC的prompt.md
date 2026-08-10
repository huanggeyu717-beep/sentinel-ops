# W5 第一段后半 · 给 CC 的 prompt

> 形状评审通过, 继续第一段。SPEC-007 已按评审结果改过一版 (末尾"第一段形状评审
> 补入"列了 7 条), **先重读第二节、第三节、第七节、第十节、第十一节**。
> 这份只写 SPEC 里没有的东西。

---

## 先说三件事

**一、迁移 0009 的原子性我实跑验过了。** 用逐字复刻你们 `env.py` 的最小复现
(单个 `begin_transaction` 包住 `run_migrations`) + 真 Postgres 16, 同序跑了一遍:
raise 之后成本列仍叫 `estimated_cost_usd`、`eval_cases` 还在、孤儿行原样、
`alembic_version` 停在 `0008`; 删掉孤儿行重跑成功。**你这一处实现是对的**,
下面第 1 条只是补一行断言, 不是修 bug。

**二、21 条样例的 DSL 合法性我逐条对过 SPEC-001** —— 上下文可用性、cooldown 下限、
参数区间、动作必需上下文, **没有发现违规**。`why` 的五处来源用得也准
(`legacy_lambda` 那几条尤其好)。

**三、你在完成报告第一节第 4 条提的那个张力, 是本周最有价值的一次异议。**
你指出的不是实现问题, 是**指标设计会自伤**。SPEC 已按它改了结构 (拆出 `repairable`),
理由写进定稿决定第 8 条。两条出路我都没选, 看 SPEC 第二节。

---

## 裁决汇总 (逐条对应你的提问)

| 你问的 | 裁决 |
|---|---|
| `mutants` 不落盘 | **采纳**, 加三条约束 (SPEC 第二节): 变异 id 由内容推导 (你写的 `scope.type:zone->sensor` 正是要的形式, **批准**)、`known_equivalent` 引用的 id 要有存在性断言、生成集合进 run 归档 |
| 两个新 kind | **采纳**, 但各加一条防退化 (SPEC 第二节): `injection_resisted` 要 `legitimate`, `dead_letter` 要断言 `discarded` |
| `tool_fault` 判据自相矛盾 | **确认是 SPEC 写错**, 按你的改法定稿: 5 条可重试 (行为等价) + 3 条不可重试 (`dead_letter`) |
| `illegal` 的张力 | **改结构**, 见上 |
| `scenarios` 按 kind 收窄 | **采纳**, 已写进 SPEC |
| 新增场景包 | **批准, 但放 `evals/scenarios/`, 不进仓库根 `scenarios/`** (理由见 SPEC 第七节)。现有三个场景一个字不许动 |
| grader 侧 zone 富化器 | **批准, 但不许是第三份映射** —— 走 `evals/fixtures/inventory.json` 单一快照 (SPEC 第七节) |
| `PROMPT_VERSION` v2→v3 | **对, 而且是必须做的**, 不是可选。已补进 SPEC 第九节修订表第 8 条 |
| 13 处不是 10 处 | 我原话就是"你自己再确认一次", 确认到更多是对的。`0001_initial.sql` 不动也对 |
| COST.md 对账 | 我的活, 不阻塞你 |

---

## 必须修的 (读代码 + 实跑复核发现的)

### 1. 迁移测试缺一行断言 (小, 一行)

`test_upgrade_with_null_task_id__aborts_without_touching_data` 断言了孤儿行和
`eval_cases`, **没断言成本列仍叫 `estimated_cost_usd`**。改名排在 raise 之前,
是这次中止里**唯一"已经执行过"的 DDL** —— 它才是这条测试最该盯的地方。
现在它只靠 `finally` 里那句 `command.upgrade` 间接兜着, 真出问题时报的是
teardown 里一句 `column does not exist`, 看不出根因。

### 2. `ambig-002` 的 `also_accept` 理由与你自己的报告第 9 条打架

理由里写"在 0 号保持沉默的一切场景上两者行为一致", 而 `scenarios` 是
`history_csv` —— 你在报告第 9 条刚查明 **CSV 里 `sensor_id` 0 有 37 行**。
所以在这条用例的判分场景上, `zone[3]` (含 0 号) 与 `sensor[5]` **并不等价**。

**根因是把 `also_accept` 当成 `known_equivalent` 在论证了。** SPEC 第二节已经把
这两个字段的语义写死:

- `also_accept` = **多个正确答案**, 理由要论证"为什么这两种读法都符合用户原话",
  **不要求彼此行为等价**;
- `known_equivalent` = 机械变异体碰巧行为等价, 理由才要论证"在这个场景上产出逐条相同"。

`ambig-003` 的理由写法是对的 (两种取舍都可辩护), `ambig-002` 的要改写。
**`also_accept` 本身保留** —— 那是 W4 真模型的真实产出, 判它错很难看,
而它确实是那句话的一种正当读法。

同一处检查一遍 `fault-002` 的 rationale ("sensor[5] 的等价性同 ambig-002 的
also_accept 论证") —— 它引的正是那段要改写的话。

### 3. 陪跑策略缺失: 四条用例的判分现在是空对空 (最要紧的一条)

`simulate` 是单策略回放。于是:

- **`combo-002`** 的 trigger 是 `incident_elapsed`。事故事件由投影器从
  `open_incident` Effect 产出, 而这条策略自己不开单 —— 单跑**一次都不触发**。
  reference 与**所有**变异体都产出空序列, grader 判"全部等价",
  **这条用例的判别力是零, 而它看起来是绿的。**
- **`simple-003` / `fault-002` / `fault-003`** 的 `close_incident` 需要 `incident_id`,
  没有事故就全落 `skipped`, 判别力也所剩无几。

SPEC-001 验收 5 早写过这件事 (那条示例策略的前置必须另有一条"变湿就开事故"的策略,
**且它的 scope 必须是 `sensor[1,2]`**, 否则两次变湿相隔 35 秒会被冷却吞掉、
事故 2 根本不存在)。SPEC-007 初稿没把它带过来, **这是我漏的, 不是你漏的**。

已补 `companions` 字段与配套的验收 6b、变异 M9, 看 SPEC 第二节。
**注意归一化规则跟着变了**: 不是直接剥 `policy_id`, 是**先按 `policy_id` 筛出
被判分那条的 Effect, 再剥**。

### 4. `simple-002` 现在不合格

你自己算过 `basic_spill` 的时间轴够不到离线判定 —— 也就是 reference 与所有变异体
一起产出空序列。新增场景已批准 (放 `evals/scenarios/`)。这条会被验收 6b 拦住,
正是它该干的事。

### 5. 缺的两批样例

- **`repairable` 3 条** (新类别, 你还不知道): 覆盖 `E_COOLDOWN_TOO_SHORT` /
  `E_ALWAYS_TRUE_CONDITION` / `E_SCOPE_IDS_MISMATCH`。
  构造要点: **输入是一句正常的人话, 只是容易诱使模型写出那个毛病**
  (例如"探头一湿就立刻给经理发邮件, 别隔太久" —— 容易让模型把 cooldown 写到
  300 以下)。**不要构造成用户主动要求一个非法值**, 那样它就变成 `illegal` 了。
- **至少一条带 `legitimate` 的 injection 样例**。你现在三条都没有,
  而"拒绝一切"是这一类的退化解。我要先看这个形状再放行批量。

---

## 剩余工作的文件边界 (在前半基础上增加)

**新增可以动**:

```
evals/scenarios/            新建, 评测专用场景包
evals/fixtures/             新建, inventory.json 库存快照
packages/scenario/          **仅限**给 loader 加路径参数这一处改动;
                            其余不动, IO 边界测试照跑
apps/api/tests/             那条"快照与 dev seed 一致"的连库测试放这里
```

**仍然不许动**: `scenarios/` (产品数据源)、`packages/policy_engine/`、
`docs/`、`README.md`、`apps/web/`。

---

## 易错点指路

1. **`companions` 的 scope 要照 SPEC-001 验收 5 的教训选。** 给 `combo-002` 配
   "变湿就开事故"的陪跑策略时, scope 必须是 `sensor[1,2]` 不能是 `zone[1]` ——
   否则两次变湿相隔 35 秒、小于冷却下限 60 秒, 第二个事故被吞, 而 `combo-002`
   的冷却分桶对照就没得比了。这个坑 W3 踩过一次, 写在 SPEC-001 验收 5 里。
2. **验收 6b 那条 lint 要在生成变异之前跑。** 它是 `companions` 缺失、场景选错
   这两类问题的统一探测器, 放在准入检查后面就晚了 —— 变异会先报一大片"全部等价",
   而真正的病因是 reference 自己什么都没产出。
3. **判别性准入的例外口子只开给 `known_equivalent`。** 遇到变异体判等价时,
   **第一反应应该是换场景, 不是加一条 `known_equivalent`**。加例外之前先问自己:
   是这两种写法真的等价, 还是我的场景太弱? `simple-003` 现在那条例外自己写着
   "定形时若换场景则删除本条" —— 那句话是对的, 请真的在换场景后回来删。
4. **`evals/scenarios/` 里的场景仍要遵守 SPEC-001 的场景包格式**, 并且
   **头部注释要写明它为哪条用例的哪个判别维度而建** —— 否则半年后没人敢动它。
5. **库存快照那条连库测试要断言"逐字一致", 不是"包含"。** dev seed 加了一个探头
   而快照没跟, 必须红。`seed_version` 也要在两边对上。
6. **`packages/scenario` 的 loader 加路径参数时, 不要破坏它现有的默认行为** ——
   `simulate_policy` 暴露给模型的枚举仍然只能是 `scenarios/` 里那三个 + `history_csv`,
   评测场景**不许进那个枚举**。这一处配一条断言。
7. **zone 富化器住在 grader 里, 不许进 `packages/policy_engine`** (零 IO + W3 冻结),
   也不许进 `packages/scenario` (那是装载器, 富化是判分侧的事)。
8. **重录 cassette 时注意 v3 Schema 的 `missing_slots` 是必填** —— 录出来的模型
   响应如果不带这个字段, 说明 prompt 里对该工具的说明没写清楚, 那是 prompt 要改,
   不要手工往 cassette 里补一个。
9. 重录仍排**最后一步**, 先报预估、人确认。手编那份 (假 zone 9) 重新手编并重标。

---

## 报告格式

前半那七节照旧 (与 SPEC 不一致 / 自行新增的分支由哪条测试守 / 数字带配置 /
用例清单 / 判别性变异结果 / M1–M10 各自的破坏手法与红灯 / 重录花费)。
本次追加两节:

8. **`companions` 用在了哪几条用例上, 每条说明"不给它搭台会怎样"** ——
   我要看到你确认过没有第二批空对空的用例混在 100 条里。
9. **验收 6b 拦下了几条**, 分别是场景选错还是缺 `companions`。
   **如果一条都没拦下, 主动说** —— 100 条里一条都没有 reference 空产出,
   大概率是这条 lint 没真的跑起来。

判别性变异那一节的自检照旧: **全部一次通过就要主动说, 那不是好消息。**

---

## 交付顺序建议

```
补 1 那行断言 + 改 2 的两处 rationale        (小, 先清掉)
  → companions 字段 + 归一化改按 policy_id 筛 + 验收 6b lint
  → evals/scenarios/ 新场景 + inventory.json + 连库一致性测试
  → 补 repairable 3 条 + 带 legitimate 的 injection 1 条   ← 停下来给我看形状
  → grader 四类判分器 + 金样
  → 判别性变异生成器 + 准入检查 (含 known_equivalent 存在性断言)
  → 剩余 76 条用例 + 数据集 lint 全绿
  → M1–M10
  → 重录 cassette (先报预估)
  → make lint + 全量测试
```

**第二个停顿点比第一个短**, 只看那 4 条的形状, 确认后直接往下走, 不用等我全读完。

---

## 老规矩

- **git 一律不执行** (`add` / `commit` / `push` / `checkout` 都不行);
  只读的要带 `--no-optional-locks`。改完列文件 + 建议 commit message, 本人来敲。
- 临时脚本放 `scripts/dev/`; 密钥只进 `.env`。
- **三条 ReplayMiss 红灯保持红着, 不许加 skip 掩盖**, 直到重录那一步。
