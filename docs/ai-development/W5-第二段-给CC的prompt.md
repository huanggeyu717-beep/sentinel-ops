# W5 第二段 · 给 CC 的 prompt

> 第一段全部收口 (226 过 / 零红灯 / 100 条定形 / 准入 69·772 全过)。
> 第二段: 能力开关 → 消融 runner → 评测库 → 成本护栏 → **真跑五臂** → 指标表。
> SPEC-007 又改过一版, 末尾新增"第二段开工前补入" 27–30 条, **先读那四条**,
> 再读第四节 (消融)、第五节 (录制回放)、第六节 (成本护栏)、第七节 (评测库)。

---

## 先说三件事

**一、你上一轮报的 SPEC 第 132 行 `model_refusal` 残留我改了**, 顺带另一处
(第 293 行) 也是同一个词。`docs/` 归我, 你报得对。

**二、重录那 7 次调用的明细比我预期有用得多, 直接改了第二段的预算。**
输入输出比是 **36:1** (不是我假设的 9:1), 每次调用 **¥0.023** (不是 ¥0.04)。
一轮从 ¥81 降到约 **¥52**, 批 ¥120 / 硬上限 ¥180。
更有用的是那个比本身: **输出 token 在这个负载里几乎免费**, 而深度思考的
reasoning token 恰恰按输出价计费 —— 2533 个 reasoning token 相当于 27 倍的正常
输出量。这是"深度思考不是免费的"最锋利的说法, `C2` 臂的结论要用这个角度写。

**三、A3 拍板不做。** 别为它纠结 —— 它要新写代码 (模型现在看不到 `ReplayReport`),
而这一段已经压着 runner + 评测库 + 护栏 + 五臂真跑。指标表里明写"未做"加一句理由。

---

## 一、本段范围

SPEC-007 分段实施的第二段。**不做 Evals 前端面板与事故报告** (第三段)。

产出顺序 (中间有**一个强制停顿点**, 见第四节):

```
AblationProfile + 两条断言 + M5
  → 评测库 (make eval-db-reset)
  → 消融 runner (走 HTTP) + run 归档格式
  → 成本护栏 (--dry-run / --max-cost-cny)
  → 跑 L0 (约 ¥2)  ← 停下来, 用真实数据重算整张预估表交给本人
  → L1 → L2 → C1 → C2
  → summary.md + COST.md 流水
```

---

## 二、文件边界

**可以动**:

```
apps/api/app/services/agent_runtime.py    能力开关穿过它 (不 fork)
apps/api/app/services/agent_prompts.py    A0 的 system prompt 变体 + v3-a0 版本号
apps/api/app/services/llm_client.py       turbo 的 model id / 按臂覆盖配置
apps/api/app/config.py                    turbo model、评测库 URL、并发度
apps/api/tests/                           新增测试
evals/runner/                             新建, 消融 runner
evals/runs/                               新建, run 归档 (进版本库)
evals/COST.md                             流水追加
Makefile / scripts/ci/                    eval-db-reset 等命令
mypy.ini / pytest.ini                     白名单
```

**不要动**:

```
docs/                     全部归评审方 (SPEC-008 在写)。SPEC 有问题写进报告
README.md                 指标表由本人改 —— 你产出 summary.md, 我搬过去
evals/datasets/           v1 已定形。要改用例必须先说, 且要写 CHANGELOG
evals/graders/            第一段已收口。发现 grader 有 bug 先停下来说
packages/                 policy_engine 冻结; scenario 只有 loader 路径参数那一处
apps/web/                 第三段
```

---

## 三、易错点指路

### AblationProfile

1. **不许 fork 状态机。** 如果 runner 里出现第二份 `agent_runtime` 的复制品,
   量的就不是交付的那份代码。开关必须穿过**同一份** runtime。
2. **两条断言缺一不可**, 而且 **M5 的靶子是第二条不是第一条**:
   - 验收 14: `AblationProfile.production()` 与 runtime 默认路径**逐字段相等**;
   - 验收 15: 打桩客户端下, `profile=production` 与**完全不传 profile** 产出
     **相同的 `agent_steps` 序列**。
   字段相等在"把某一档开关写死成 production 的值"这个变异下**可能照样绿**
   (改的恰好是一个本来就为 True 的开关), 步骤序列才是真正的守卫。
3. **A0 的 system prompt 用 `prompt_version = "v3-a0"`**, A1 / A2 共用 `v3`。
   不分开的话同一个版本号下会有两种 system prompt, **回放键串味**,
   `ai_usage.prompt_version` 那一列也分不清档位。
4. A0 的资源清单塞进 prompt 时, **从 `evals/fixtures/inventory.json` 取还是从
   工具的真实返回取?** 定为**从真实的只读 service 取**, 与 A1 的工具同源 ——
   两边内容不一致的话, A0→A1 的差就混进了"清单本身不一样"这个无关变量。
   快照那份是给离线 grader 用的, 不是给 prompt 用的。

### 评测库

5. `make eval-db-reset` = drop + create + 让 API 启动跑迁移 + 写 dev seed。
   **确认 `apply_dev_seed=true`**, 并**确认 dev seed 里没有随机数、没有依赖
   当前时间且会影响 inventory 的东西** —— 有的话当场说, 不要自己改种子。
6. **每臂开跑前重置**, 这同时也是 `agent_tasks_one_open` 撞索引那个坑的解药
   (16 条 ambiguous 会停在 `clarifying`, 不重置的话第二臂说同一句话会拿回
   第一臂的任务, 而**没有任何东西会报错**)。
7. 重置后**校验 inventory 与 `evals/fixtures/inventory.json` 一致** ——
   那条连库测试已经有了, 在 reset 之后跑一次即可; 不一致就停, 别开跑。

### runner

8. **走 HTTP (`POST /agent-tasks`), 不直接调 service。** 绕过 HTTP 就绕过了
   权限层、去重层、并发预留槽位, 量的不是交付的那条路。
9. **并发会撞 429。** `agent_max_concurrent_tasks` 默认 4, 超出时
   `POST /agent-tasks` 直接返回 429 不建行。runner 要自己限流到这个数并对 429
   退避重试, **不要把 429 当成用例失败**。
10. **并发度进 run 快照, 而且 P50/P95 要报两个数** (SPEC 第一节第 3 项):
    端到端墙钟 (混着排队) 与纯模型时间 (按 `task_id` 汇总 `ai_usage.latency_ms`)。
    两者之差是编排开销, 本身是个该报的数字。只报一个的话换个并发度数字就变,
    而没人看得出为什么。
11. **clarify 类要自动回答**: 收到 `clarifying` 就用用例里冻结的 `clarify_answer`
    调 reply。注意"只有发起人能回答", runner 全程用同一个评测账号。
    **回答等待的那段时间要从端到端墙钟里扣掉** (SPEC 第一节第 3 项)。
12. **失败任务照样进分母**, `failed` / `dead_letter` 不许从统计里剔掉。

### 成本护栏 (这是唯一会真花钱的一段代码)

13. **`--dry-run` 必须先跑**: 打印用例数 × 预估调用数 = 总调用、× 单价 = 预估花费,
    连同完整配置快照, **要人显式输入确认才继续**。
    配一条测试: dry-run 用 `MockTransport` 断言**零网络请求**。
14. **`--max-cost-cny` 超限当场停, 并归档已完成部分**, 不是丢弃、不是跑完再看账单。
15. **真花钱的模式必须由命令行显式给出, 不读 `config.llm_replay_mode` 默认值** ——
    免得有人 config 恰好是 `record` 就默默开跑。
16. 实测臂用 **`record`** (真调用 + 落盘轨迹), 不是 `off`; CI 只跑 `replay`。
17. **cassette 目录按 run 分**: 给 `RecordReplayLLMClient` 传
    `.llm-cache/<run_id>/`, 不改它的目录逻辑。**跑完 L0 先量一次实际体积**,
    再决定全量进不进版本库 (第一段那 7 个平均 3.8 KB, 但评测的 messages 更长)。

### 真跑

18. **L0 之后必须停。** 它只花约 ¥2, 是整条流水线 (runner / 归档 / 统计 / 护栏)
    的第一次真实检验。跑完把**真实调用数与真实均价**填回预估表, 重算 L1–C2,
    交给本人过目再往下。**不要拿 L2 当第一次试跑。**
19. **`C2` (思考开) 要单独放宽 `agent_llm_timeout_seconds` 到 180 秒**,
    样本是 core 40 里按类别等比抽的 20 条, **抽法确定性 (按 id 排序取前 N)**,
    抽中的 id 列表进 run 快照。这一臂报出来的成功率**不是出厂配置下的成功率**,
    summary 里要写明。
20. **`C1` (turbo) 的 model id 加一个新配置项, 不要改 `llm_model` 的默认值** ——
    默认值是出厂配置, 改了它 W4 那批数字的口径就变了。
21. **回放 miss 不许静默跳过**: 整条用例判失败并计进报告。miss 率本身要报。

---

## 四、强制停顿点

**跑完 `L0` 停下来**, 报这几样再往下:

- L0 的真实调用数、真实 tokens (入/出分开)、真实花费、真实墙钟;
- 用真实均价重算的 L1 / L2 / C1 / C2 预估表;
- 一个 cassette 目录的实际体积, 以及"全量进不进版本库"的建议;
- 流水线本身有没有暴露问题 (归档格式够不够、统计口径对不对)。

**这个停顿点是刻意的**, 请真的停一下。后面四臂加起来约 ¥50, 是这个停顿的十倍。

---

## 五、报告格式

前几轮那几节照旧 (与 SPEC 不一致 / 自行新增的分支由哪条测试守 / 数字带配置 /
变异手法与红灯)。本段追加:

14. **M5 的破坏手法与红灯输出** —— 靶子是验收 15 不是验收 14, 报清楚你破坏的是
    哪一档开关、以及验收 14 在同一个变异下是红还是绿 (**若它照样绿, 原样上报**,
    那正是 SPEC 特别标注它的原因)。
15. **五臂的完整指标表**, 每一格带配置。分类别的表是主体, 总分只作摘要;
    macro 与 micro 都给。
16. **拦截层次分布表** (`model_clarified` / `model_protocol_error` / `schema` /
    `static_validator` / `replay_warning` / `none`) —— 这一张比总分值钱。
17. **注入得逞率必须是 0%**, 不是的话停下来报, 不要继续跑后面的臂。
18. **回放 miss 率** 与 **cassette 实际体积**。
19. **`COST.md` 流水每臂一行**, 并在末尾给"本轮累计 vs 批准额度 ¥120"。

---

## 六、老规矩

- **git 一律不执行**。上一轮踩过: **`checkout` 不管带不带参数都不算只读**,
  `--no-optional-locks` 也救不了它。还原文件用 `cp`。
- 临时脚本放 `scripts/dev/`; **密钥只进 `.env`, 不进代码、注释、cassette、报告**。
- 发现 `evals/graders/` 或 `evals/datasets/` 有问题 **先停下来说**, 不要自己改 ——
  第一段已经收口, 改了它历史分数就不可比了。
- 报数字必须带上产生它的配置 (SPEC 第一节那 11 项, 缺一项汇总函数会直接报错)。
