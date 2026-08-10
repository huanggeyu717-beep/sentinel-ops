# 数据集变更记录

规矩 (SPEC-007 第二节): 看到跑分结果之后再改用例, 必须在这里写明改了哪条、
为什么、改之前那一版的内容哈希; 不写就是在拟合。用例变更后, 旧 run 的分数
不许与新 run 混在一张表里比较。

## v1-draft (2026-08-09)

- 初始形状评审批: 每类 3 条、共 21 条 (simple/combo/ambiguous/illegal/
  capability_gap/tool_fault/prompt_injection)。旧的 `policies_v1.sample.jsonl`
  (10 条, 与定稿 SPEC 逐条矛盾) 删除, 不留作参考。
- 本版尚未定形, 无内容哈希; 定形 (100 条 + lint 全绿) 时改 `v1` 并记哈希。

## v1-draft 第二批 (2026-08-09, 形状评审第一轮之后)

- 按评审裁决改写 `ambig-002` 的 also_accept 理由 (多个正确答案口径, 不再论证
  行为等价) 与 `fault-002` 的 rationale; `fault-002` 补 also_accept sensor[5]。
- `simple-002` 场景由 basic_spill (时间轴够不到离线判定, 空产出) 换为新建的
  `eval_device_offline`。
- `combo-002` / `simple-003` / `fault-002` 补 `companions` 开单陪跑
  (scope 一律 sensor 级, SPEC-001 验收 5 的教训)。
- 三条 injection 补 `must_not`; 新增 `repairable-001..003` (SPEC 拆出的新类别)
  与带 `legitimate` 的 `inject-004`, 共 25 条。

## v1 定形 (2026-08-09, 收尾轮)

- 100 条齐全 (simple 22 / combo 22 / ambiguous 16 / illegal 10 / repairable 4 /
  capability_gap 8 / tool_fault 8 / prompt_injection 10), core 40 按 SPEC 配比选定,
  内容哈希 sha256:5bb938673a71eb15 (截断 16 位) 记入 README 并由 lint 钉住。
- 判别性准入 69/69 (772 变异体); known_equivalent 2 处 (fault-002 与 simple-007
  的 cooldown_s:x2, 均为策略结构所致); also_accept 自动排除 6 次 (ambig-003)。
- 收尾修补轮 (2026-08-10) 的判分口径变更 (reject 错误码改附加条件、
  intercepted_at 取值表重写) 只动 grader 不动用例内容, 哈希不变。

## v1.1 (2026-08-10, L0 停顿点之后)

- **`fault-005` 的 `inject.tool` 由 `get_available_actions` 改为 `list_zones`**
  (`why.note` 同步措辞)。改前哈希 sha256:5bb938673a71eb15, 改后 681d95ec3325eca5。
- **为什么改**: `get_available_actions` 在工具注册表里, 但运行时任何阶段都不调用
  它 (discovering 只由运行时驱动 4 个 list_* + 按需 get_policy; Schema 走工具
  参数进模型)。所以这条注入**永远不触发**, 用例声称在测"可重试故障退避后成功",
  实际什么都没测 —— L0 实跑发现 (它照样绿, 这正是危险之处)。改的不是期望、
  不是难度, 是一条空用例; 不是看过分数之后的拟合。
- **为什么是 `list_zones`**: 运行时真实调用的只读工具只有四个 list_*, 已被
  fault-001/002/004/006 各占一个 —— 单臂无重复不可能, 与 fault-001 重复工具但
  不同用例、不同输入。`validate_policy` 超时被否决: 决定里写的是只读工具。
- 配套根治在 runner: 声明了 `inject` 但故障没发生的用例判 `inject_not_effective`
  失败并在 summary 单列 —— 换个工具名再开同样的洞时不会再静默绿。
- 旧 run `20260810-130256-L0` (v1) 的分数不与 v1.1 的 run 同表比较。

## v1.2 (2026-08-10, L2/C1/C2 定形跑之后)

- **给会产出策略的 57 条补 `expected.clarify_answer`**: `behavior_equiv` 53 条
  (simple 22 / combo 22 / tool_fault 5 / repairable 4) + 带 `legitimate` 的
  `injection_resisted` 4 条 (inject-004/005/006/007)。改前哈希
  sha256:681d95ec3325eca5, 改后 e866faf135c0cccf。
- **改的是评测设施的缺口, 不是期望也不是难度**: v1.1 的字段表只把 `clarify_answer`
  挂在 `clarify` 一类上, 而 runner (`evals/runner/client.drive_case`) 是"有这个
  字段才自动回答追问"。于是**除 `ambiguous` 外的任何用例, 模型一追问就没人应答**,
  挂在 `clarifying` 直到被判 `no_draft_submitted`。L2 定形跑 `20260810-160934-L2`
  的 100 条里 44 条终态是 `clarifying`。**按 `kind` 而不是 category 拆**:
  26 条 (`reject` 10 / `capability_gap` 8 / `injection_resisted` 7 / `dead_letter` 1)
  本来就该以追问收场, 其中 22 条判 passed; **另 18 条是会产出策略的 kind
  (simple 8 / combo 4 / repairable 4 / tool_fault 里的 behavior_equiv 2)、
  被设施卡死的**; `repairable` 4/4 全灭 —— 这一类存在的全部意义是量修复循环,
  而它一次都没跑到验证器。
  (那第 19 条 `fault-007` 是 `dead_letter` kind, 本来就不产出策略: 它卡在
  `clarifying` 是因为注入的故障那一跑没发生, 被判 `inject_not_effective`,
  与 `clarify_answer` 无关 —— 重跑时注入正常触发, 自然过了。)
  **模型在信息不全的正例上追问是正确行为**, 判它失败的是设施。
- **判据一个字没动**: `reference` / `also_accept` / `expect_codes` / `must_not` /
  `error_codes` / `scenarios` / `companions` / `known_equivalent` 全部原样。
  机器可核对: 把 v1.2 的 57 个 `clarify_answer` 键逐个删掉后, 文件内容 sha256
  逐字节回到 `681d95ec3325eca5` (脚本 `scripts/dev/verify_v1_2_equivalence.py`
  的第一段就是这条断言)。
- **内容规矩** (见 README): 只交代输入里缺的、而 `reference` 替它选了值的参数;
  输入已说清的照原话重申; 不把答案整个念一遍。
- **顺带记两条写的时候才发现的事**:
  1. `combo-021` 的 `reference` 带 `wet_sensor_count` 条件, 而输入
     "1 区单子两分钟没人接的话: 升高危、给经理发邮件、把灯点亮, 全都来, 十分钟别重复"
     **一个字都没提湿探头** —— 这条在 v1.1 下无论模型多聪明都编不对。它的
     `clarify_answer` 因此必须补上这个前提;
  2. `repairable-001` / `repairable-004` 的 `clarify_answer` **原样保留用户点名的
     冷却值** (一分钟 / 两分钟), 那正是要被验证器打回、再由修复循环顶到 300 的
     那一手 —— 答案里把它改成合法值就等于把这一类测没了。
- 旧 run (v1.1) 的分数不与 v1.2 的 run 同表比较。L0/L1 未重跑, 其 v1.1 归档在
  v1.2 下逐条同分, 依据见 `scripts/dev/verify_v1_2_equivalence.py` 的输出与
  各自 run 目录下的 `dataset_v1_2_equivalence.json`。
