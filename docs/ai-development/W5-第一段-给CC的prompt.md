# W5 第一段 · 给 CC 的 prompt

> 这份只写 SPEC 里没有的东西: 读什么、动哪些文件、易错点在哪、报告怎么写。
> **不复述 SPEC 内容** —— 复述一遍就是同一个事实存两份, 改了 SPEC 这边不跟着改就走散。

---

## 先读这几份, 按顺序

1. `CLAUDE.md` —— 架构不变量与协作红线
2. **`docs/specs/SPEC-007-evals-and-ablation.md`** —— 本段的全部依据, 通读
3. `docs/specs/SPEC-001-policy-dsl.md` —— 第二节 (Policy 结构)、第三节 (Effect)、
   第五节 (错误码与 Inventory)、第六节 (回放报告与警告码)。
   **写答案键与 grader 全靠这几节**
4. `docs/specs/SPEC-002-agent-orchestration.md` —— 第五节 (工具清单)、
   第八节 (预算与思考开关)、第九节 (录制回放三条规矩)
5. `docs/进度与交接.md` —— W4 那一章与"工作方法"那一节

## 本段做什么

SPEC-007 的"分段实施 · 第一段"。**不要提前做第二段的东西** (消融 runner、评测库、
真跑) —— 这一段的产出必须能在完全离线的情况下验收。

## 文件边界

**可以动**:

```
apps/api/alembic/versions/0009_*.py          新建
apps/api/app/services/llm_client.py          temperature + 成本字段改名
apps/api/app/services/agent_tools.py         ask_clarification 加 missing_slots
apps/api/app/services/agent_runtime.py       同上, 以及落库那一步
apps/api/app/services/agent_service.py       ai_usage 写入的列名 + 澄清落库
apps/api/app/services/agent_prompts.py       该工具的说明
apps/api/app/config.py                       单价注释里的币种说明
apps/api/tests/                              新增测试
apps/api/tests/cassettes/                    重录 (最后一步)
evals/                                       整个目录, 旧文件该删就删
scripts/dev/                                 需要临时脚本放这里
mypy.ini  pytest.ini  .env.example  Makefile 白名单与命令
```

**不要动**:

```
docs/                    评审方在写 SPEC-008, 会撞车。
                         SPEC 有问题写进报告, 不要自己改
README.md                指标表等第二段有真数字了再改
packages/policy_engine/  W3 冻结, 且有零 IO 断言。grader 要用它就 import,
                         不要为了方便往里塞东西
packages/scenario/       同上, 场景装载走它现有的 loader
apps/web/                第三段的事
```

**如果你认为冻结的文件里有真 bug, 先停下来说, 不要自己改。**

## 易错点指路

### 迁移 0009

1. **先确认 head 是 `0008_agent_orchestration`**, 不要凭文件名排序猜 (W2 换 Alembic
   就是为了这个)。
2. **`agent_steps.task_id` 收 NOT NULL 之前先查有没有 NULL 行。有就直接 raise 中止**,
   不许 DELETE、不许填假值。静默改数据比报错难查一百倍。
3. **删那三张 eval 表的 `downgrade`, 从 `apps/api/migrations/0001_initial.sql`
   第 120–132 行原样抄 DDL**, 不许凭记忆重写 —— W2 立过"基线不翻写"的规矩,
   24 张表抄漏一个约束不会有任何报错。
4. 约束名不要猜, 从 `pg_constraint` 查实名 (W3 第二段的做法, 查到零个或多个都报错)。

### 成本列改名

5. `estimated_cost_usd` 这个名字在 **10 处代码 + 7 个 cassette + 2 处测试**里出现过,
   我已经 grep 过一遍, 你自己再确认一次。其中**有一处是隐蔽的坑**:

   `llm_client.RecordReplayLLMClient.complete()` 回放分支里那句
   `recorded.pop("estimated_cost_usd", None)`, 后面紧接着 `LLMResponse(**recorded, ...)`。
   **改名之后如果 pop 的字符串没跟着改, 老 cassette 里那个键会作为未知关键字参数
   传进 `LLMResponse`, 直接 TypeError。** 虽然这一段最后要把 7 个 cassette 全部重录、
   老键不会再出现, 但**别指望重录来掩盖这个 bug** —— 请把这一处显式处理干净。

6. `.env.example` 与 `config.py` 里"这一列存的是人民币"那几句注释, 改名之后**要跟着改口径**,
   不要留一句"暂时落在名叫 usd 的列里"的过期说明。**过期注释被当成事实**是这个项目
   本周内出现过三次的归因错误 (`sensor 0` / viewer / 变异 21)。

### temperature

7. 温度进 `cassette_key` 之后, **现有 7 个 cassette 全部失效, 依赖它们的测试会集体变红。
   这是预期的, 不是你写坏了。** 重录排在本段**最后一步**。
8. 重录**复用 `scripts/dev/record_cassettes.py`**, 不要新写一个。它是 W4 第二段写的,
   已经跑通过。
9. **那个手编的 cassette (把真实输出的 `{"type":"sensor","ids":[5]}` 改成不存在的 zone 9,
   用来稳定复现验收 4 的修复循环) 要重新手编一遍**, 并重新写 `hand_edited` 字段说明改了什么。
   不标注的话下一个人会把它当成模型的真实行为去分析。
10. 重录是本段**唯一一次真花钱**的操作 (约 7 次调用, 不到 ¥0.3)。
    **先打印预估、要人确认, 再执行**; 完成后把实际次数与花费写进 `evals/COST.md`。
    `.env` 里的 key 我已经填好了, **不要把 key 打印到任何地方**。

### missing_slots

11. 这一处改动要同步**五个地方**, SPEC-007 第九节列了。
    最容易漏的是**落库那一处** —— 前四处全做对、这一处漏掉的话, 参数只活在内存里,
    `ambiguous` 那 16 条判分读不到任何东西, 而且**不会报错**。
    验收 23 与变异 M8 就是为这一处准备的。
12. 工具清单与注册表那条相等断言测试会被这次改动碰到, 注意别绕过它。
13. `missing_slots` 是 `text[]`。写入时**要在 service 层校验取值都在枚举内**,
    不要信任模型输入 (`CLAUDE.md` 不变量 5 的同一条道理)。

### 数据集与 grader

14. **100 条一次性交出来风险太大。请分两批:**
    **先交结构 + 每个类别 3 条样例** (共 21 条) 给评审方看形状,
    确认之后再批量产剩下的。做完 100 条才发现字段形状不对要全推倒。
15. `.jsonl` **一行一条, 不要 pretty print** (diff 才看得清改了哪条);
    中文不转义 (`ensure_ascii=False`)。
16. **旧的 `evals/datasets/policies_v1.sample.jsonl` 删掉, 不留作参考。**
    它里面至少 7 条与现在的 SPEC 矛盾, 留着就是"两份走散"。
    删完 grep 一遍确认没有别处引用它 (范围限定代码与数据, 文档正文里提到它是正常的)。
17. `evals/graders/behavior_grader.py` 现在是 `raise NotImplementedError("W5")`,
    它的 docstring 是 W1 写的、**"过程指标(读 agent_steps)"那句现在不准**
    (过程指标归第二段的 runner)。实现时把 docstring 一并改准。
18. grader 要跑场景 → **场景装载调 `packages/scenario` 的 loader**
    (那个包允许读文件), grader 自己不要拼路径读 YAML。
19. 判别性变异的规模: 100 条 × 约 20 个变异体 × 场景数 次 `evaluate()`。
    单次实测 14–16 ms, 所以总量在一分钟量级, 可以接受 —— **但别写成 O(n²)**
    (比如每个变异体都重新装载一遍场景)。场景事件列表装载一次、复用。
20. `evals/` 下的新模块要进 `mypy.ini` 严格档白名单, 否则新代码悄悄退回默认档;
    也要确认 `pytest.ini` 收得到 `evals/` 下的测试。
21. ruff 配置在仓库根 `ruff.toml` (line-length 100, 中文全角标点的 RUF001/002/003
    已整体 ignore), 不要在 `evals/` 下另立配置 —— ruff 按"离文件最近的配置"生效,
    分散配置会让不同目录悄悄用不同规则 (W1 的 ADR-005)。

### 答案键

22. **每条 `reference` 写完先过静态验证器**, 别等 lint 阶段一起报。
23. **`scope` 与 `cooldown_s` 这两处必须逐条写"为什么这么选"。** 这是最容易出现
    "两种写法都对"的地方 —— 例如"任何传感器湿了就点灯", `scope: global` 与
    `scope: sensor[...]` 都符合字面, 但前者全局一个冷却桶, 1 区湿了会吞掉后场
    (W3 修过的那个坑)。这类要么给 `also_accept`, 要么改判为 `ambiguous`,
    **不要自己挑一个然后不说**。
24. **不要用被测模型 (Doubao) 生成答案键。** 本段全程不调用它, 重录 cassette 除外。

## 报告格式 (固定几节, 一节都不能省)

1. **与 SPEC 不一致的地方, 逐条列出** —— 包括你认为 SPEC-007 或本 prompt 写错的地方。
   这一条的用意是把"偏离"从错误变成可以正大光明汇报的东西; 前四周你据此纠正过评审方两次。
2. **自行新增的分支 / 写操作, 逐条说明它由哪条测试守着。** W4 连着两次栽在同一件事上:
   自己加的兜底分支一条测试都没有。
3. **报任何数字必须带上产生它的配置**, 否则复现不了也就核对不了。
4. **100 条用例的分类别清单**, 每条一行: `id / category / core / input 摘要 /
   expected.kind`。另附一份 `reference` 的选型说明 (第 23 条要求的那些)。
   量大的话单独放一个文件, 报告里给路径。
5. **判别性变异的结果**: 多少条用例一次通过、多少条因为场景没判别力被打回换了场景、
   `known_equivalent` 用了几处分别为什么。
   **如果 100 条全部一次通过, 请主动说出来** —— 那大概率意味着这个检查根本没生效
   (变异没真的被跑, 或者场景太丰富), 不要当成好消息报上来。
6. **七个变异测试 (M1–M8) 各自的破坏手法与红灯截图/输出。**
   如果某条变异下测试**照样绿**, 原样上报, 不要偷偷补一条测试再报绿 ——
   W4 第一段变异 22 就是这么处理的, 处理得对。
7. **重录 cassette 的实际调用次数与花费**, 以及写进 `COST.md` 的那一行。

## 交付顺序建议

```
迁移 0009 (含降一步再升回的验证)
  → temperature + 成本改名 (先不重录, 让测试红着, 报告里说明)
  → missing_slots 五处同步 + 验收 23
  → 数据集结构 + 每类 3 条样例  ← 停下来交给评审方看形状
  → grader + 金样 + 判别性变异准入
  → 剩余 79 条用例 + lint
  → 变异测试 M1–M8
  → 重录 cassette (唯一花钱的一步, 先报预估)
  → make lint + 全量测试
```

**中间那个停顿点是刻意的**, 请真的停一下。

## 最后

- **git 命令一律不要执行** (`add` / `commit` / `push` / `checkout` 都不行)。
  只读的可以, 但必须带 `--no-optional-locks`, 例如
  `git --no-optional-locks status --porcelain`。原因是机械的: 普通 `git status`
  会拿 `.git/index.lock` 再删掉, 而文件桥接这条路只让写不让删, 锁拿得到还不回去,
  下一条 git 就报 `Unable to create index.lock`。W4 踩过一次。
  改完列出改了哪些文件、给一条建议的 commit message, 由本人执行。
- **临时脚本放 `scripts/dev/`**, 不要写进系统临时目录。
- **密钥只进 `.env`**, 不进代码、注释、cassette、报告。
