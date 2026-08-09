# W4 第二段 给 CC 的 prompt

第一段与修补均已复核通过。本文件只写 SPEC 里没有的东西：文件边界、命名落位、
易错点指路、报告格式。**SPEC 里已经写过的内容一个字都不复述。**

依据是 `docs/specs/SPEC-002-agent-orchestration.md`，**该文档在你交完修补之后又改过**
（第三节上限表加了"单次 LLM 调用 60 秒"这一格、第九节补了录制回放的三条落地规矩、
第二节与验收 15c/变异 22c 有更新）。**开工前请重读第二、三、九节与第十二节验收**。

## 任务

实现 SPEC-002 的**第二段**（见该文档末尾"分段实施"）。外加开头两件小事。

---

## 开工前先做的两件小事（第一段修补的复核发现，量很小）

**一、四处盖戳里，新建那一处没有测试守着。**

按 SPEC-002 验收 15c 第三条与变异 22c 落地。**为什么不能靠已有那条代劳，SPEC 里
写了原因**（`NULL - interval` 仍是 `NULL`），照它做即可。复核实测确认过：删掉新建
那一处盖戳，现在 174 条一条都不会红。

**二、`heartbeat_at IS NULL` 那一格的 `error_detail` 要与真失联分开。**

按 SPEC-002 第二节新写的那段落地。现在两种情况共用"任务失联, 可能是服务重启或
进程异常"这句，而按你自己在修补里定的新语义，NULL 的含义是"有人写了一处让任务处于
running 却忘了盖戳"，跟重启毫无关系——排查的人会去翻根本没发生过的重启记录。

---

## 文件边界

**归你，可以自由新建和修改：**

- `apps/api/app/services/llm_client.py`（真实方舟客户端 + 录制回放）
- `apps/api/app/services/agent_prompts.py`（**新建**，见下"命名与落位"）
- `apps/api/app/services/agent_runtime.py`（改 `_messages` 的去处、prompt 版本号、
  模型输出解析的错误归类）
- `apps/api/app/services/agent_service.py`（**只允许**改上面那句 `error_detail`）
- `apps/api/app/config.py`
- `apps/api/tests/test_agent_llm.py`（新建）、`apps/api/tests/cassettes/`（新建目录）
- 已有的 `apps/api/tests/test_agent_*.py`
- 仓库根 `.gitignore`、`mypy.ini`

**冻结，一律不动**：`packages/**`、`apps/api/app/routers/**`、`apps/web/**`、
`policy_service.py`、其余 `app/services/*.py`、`docs/**`。

**本段仍不做**：HTTP 路由与 SSE（第三段）。

## 命名与落位

- **prompt 单独成模块** `agent_prompts.py`，不留在 `agent_runtime._messages` 里。
  理由：prompt 版本号是 W5 消融实验的自变量之一，改 prompt 要能一眼看出改了什么、
  换没换号；混在状态机文件里，`git log` 会把"改状态机"和"改 prompt"搅在一起。
  版本号常量也放这里，`stub-v0` → 定稿时换 `v1`。
- **两个录制目录，分工写进 `.gitignore` 的注释**：
  - `apps/api/tests/cassettes/`——测试与将来 W5 评测依赖的录制，**进版本库**；
  - `.llm-cache/`（`SENTINEL_LLM_RECORD_REPLAY_DIR` 默认值）——开发时随手产生的，
    **不进版本库**。
- 配置项：`agent_llm_timeout_seconds`（60）、`llm_replay_mode`（`record`/`replay`/`off`）、
  `llm_price_input_per_mtok` / `llm_price_output_per_mtok`。

## 易错点指路

**录制回放（本段最容易做错的地方）**

- **键里绝不能有 `task_id`、时间戳、随机串。** 这类东西每次都不一样，键跟着变，
  回放永远命中不了——而症状特别阴险：**跑得通，只是每次都在真花钱**，你不会看到
  任何报错。录完之后请**显式验证一次**：同一条任务连跑两遍，第二遍
  `ai_usage.cache_hit` 必须全为 true。
- 键里的 `messages` 含 inventory（区/传感器/角色），那是从数据库来的。请确认在同一套
  dev seed 下这些内容是确定的（id 与顺序都稳定），不确定就在算键之前先归一化。
- **`replay` 模式下没命中就直接失败**，不要回退去调真模型。CI 固定 `replay`。
- **`ai_usage.cache_hit` 要真填**（回放命中为 true）。这一列 W5 算成本要用，
  现在是常量 false。

**真实模型才会出现、打桩下永远看不到的失败**

- 方舟是 OpenAI 兼容协议，`tool_calls` 的 `arguments` 是一个 **JSON 字符串**，
  要解析，而且**可能不是合法 JSON**。这是真模型最常见的失败模式。归到你自己在
  第一段定的 `model_protocol_error` 那一格，不要让它变成一个裸的 `JSONDecodeError`
  冒到 `_tool_step` 的通用兜底里去（那会落 `dead_letter`，口径就错了）。
- 模型可能调一个**存在但本阶段没给它**的工具，也可能**返回空 tool_calls 却又不给
  文本**。两种都走同一格。
- 单次 LLM 调用要有自己的超时（SPEC 第三节新加的那一格），别指望工具超时管它——
  那两个是不同的东西。

**验收 4（修复循环）的录制怎么造**

真模型不一定第一次就产出一个引用了不存在 zone 的草案。允许你**手工编辑 cassette**
把它造出来，但**必须在那份 cassette 里就地标注它是手编的、以及手编了哪一段**。
不标的话，下一个人会把它当成模型的真实行为去分析，得出错误结论。

**密钥**

- 不要把 key 写进任何会提交的文件；确认 `.env` 在 `.gitignore` 里。
- **加一条测试断言 `apps/api/tests/cassettes/` 下的内容不含 key 的值**——
  key 走 header 本来就不该进请求体录制，但这条断言是白拿的保险。
  仓库红线是"密钥任何情况下不进仓库"。

**成本估算**

- `estimated_cost_usd` 现在写死 0。单价放 config（每百万 token 输入/输出各一个数），
  不要硬编码在代码里，并就地注释一句：**价目会变，这个数字只用于相对比较，
  不是财务口径。**

**测试里不许真连网**

- 例外只有一条：一个默认 `skip`、只有显式给了 key 才跑的冒烟测试。CI 上不跑。

## 120 秒预算的回填

按 SPEC-002 第八节那一段做，另加一条：**必须写明这五次是在真实调用上量的，
不是在回放上量的**。回放几乎是 0 毫秒，拿它回填等于没量。数字带上模型名与
prompt 版本，格式照第八节那张表。

## 完成报告

沿用前两次的八节格式。这次特别要有的：

1. **回放键由哪些字段算出来，逐条列。** 以及"同一条任务连跑两遍、第二遍
   `cache_hit` 全为 true"的实测输出——这是证明录制回放真的在工作的唯一硬证据。
2. **哪些 cassette 是手工编辑过的，逐条列**，各自改了什么、为什么。
3. **120 秒回填的五次原始数字**，带模型名与 prompt 版本，并注明是真实调用。
4. **第一次跑通真实模型的那条任务的完整 Trace**（按 seq 排好的时间线），贴出来。
   这一段最值得看的东西就是它——一句人话到一条待审批的策略，中间每一步。
5. **自行新增的分支/写操作，逐条说明它由哪条测试守着。**（前两轮两次都栽在这上面：
   第一次是新加的判死分支没测试，第二次是新加的盖戳没测试。这条从现在起固定要有。）
