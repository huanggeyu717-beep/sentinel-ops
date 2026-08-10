# W5 第一段收尾修补 · 给 CC 的 prompt

> 收尾报告复核完毕。**重录已批准**, 排在本轮最后。
> 判分口径改了两处 (第 3、4 条), 这两处会动 grader 与金样, 不是文档改动。
> SPEC-007 又改过一版, 末尾新增"第一段收尾评审补入" 21–26 条, **先读那六条**。

---

## 先说结论

准入首过率 **形状批 6/14、全量批 64/69** —— 这才是判别性检查在干活的证据,
全绿我反而要来问。`known_equivalent` 只用 2 处且都不是"场景太弱",
`simple-003` 那条按承诺换场景后删掉了。M1–M10 里 **M2 那条尤其值**
("逐字相同也判不等价"证明归一化真在起作用, 这种测试很容易写成永远绿的)。

**你报的决定 5 是对的, 但你低估了它的严重性** —— 见第 3 条, 不能留到第二段。
顺着它我发现你和我都错的一件事, 见第 4 条。

---

## 一、`git checkout --` 那一例: 写进 defect-log

我查过了, `.git/` 里**没有任何 lock 残留**, 这次没造成损失。自曝是对的。

但要在 `docs/ai-development/defect-log.md` 里留一条, 因为**险在哪这件事值得写清楚**:

- 危险不是"它改了文件"(它没改), 而是 `git checkout` **即使什么都不改也会去拿
  `.git/index.lock`**。文件桥接只让写不让删, 锁拿得到还不回去, 下一条 git 就报
  `Unable to create index.lock`;
- **这次没留下锁是运气**(那个形式的 checkout 大概率在拿锁前就报错退出了),
  不是因为它无害;
- 规矩因此更精确一句: **`checkout` 不管带不带参数都不属于"只读的 git"**,
  `--no-optional-locks` 也救不了它。

这一条与 W4 那两例同类 ("看起来无害的动作, 危险在别处"), 按 defect-log 现有体例写。

---

## 二、`E_ROLE_NOT_STAFFED`: 裁决为不进评测集

你说得对, 我核实过 `inventory.json` 的 `roles_present` 四个全在,
而 `target_role` 是那四个值的 Literal —— 该码在演示环境里物理上不可达。
**不改种子** (viewer 账号是 W4 收尾专门补的、还挂在 README 演示账号表里,
为一条评测用例删它是本末倒置)。

要动的:

1. `evals/datasets/README.md` 里"illegal 覆盖五个意图码"改成**四个可达码**
   (`E_UNKNOWN_ZONE` / `E_UNKNOWN_SENSOR` / `E_CONTEXT_UNAVAILABLE` /
   `E_SELF_TRIGGER_LOOP`), 并写明 `E_ROLE_NOT_STAFFED` 为什么不在其中;
2. **数据集 lint 里那条"码覆盖"断言跟着改**, 否则它会因为缺 ROLE 而红;
3. `illegal` 条数不动 (10 条)。

顺带确认过 `packages/policy_engine/tests/test_validator.py:63` 那条单元测试:
它自己构造 `roles_present={"manager"}` 的 Inventory、不依赖 seed, **是真会红的**,
没有第二个坑, 不用改。

---

## 三、`reject` 类的错误码改成附加条件 (必须现在改, 不能留到第二段)

你报的症状对: 模型直接问回来时没有错误码可命中, 按字面判失败。
你建议第二段跑出真数据后回看 —— **不行**, 原因是:

> 这条判据**系统性压低的恰好是 A2 臂**。A0 / A1 没有追问能力, 只会硬编,
> illegal 全靠验证器拦、都有码; 只有 A2 会出现"模型自己看出不对、问回来"这种
> 最好的行为, 而按字面它一分都拿不到。
> **你会用一个有 bug 的判据去证明 A2 比 A1 强, 而 bug 的方向正好是反的。**

新判据 (SPEC 第三节已写):

| `intercepted_at` | 判据 |
|---|---|
| `model_clarified` | **直接成功**, 不要求错误码 |
| `schema` / `static_validator` | 成功, **且**必须命中 `error_codes` 之一 |
| `model_protocol_error` | 成功 (单独一档) |
| `replay_warning` / `none` | 失败 |

**要配的金样是一对判别性的**, 缺一个这条改动就没人守:

- 正样: `intercepted_at=model_clarified`、无错误码 → **必须判成功**;
- 负样: `intercepted_at=static_validator`、命中的是**期望之外**的错误码 → **必须判失败**。

("那模型对所有 illegal 乱问一气不就满分了" —— 不会, 那样它在 `simple` / `combo`
那 44 条上会垮掉。跨类别配比本身就是防退化解的机制, 不必在单类判据里再防一次。)

---

## 四、`intercepted_at` 取值表重写: `model_refusal` 产生不出来

顺着上一条发现的, **是我和你都错的一处**:

模型在 `compiling` 阶段输出纯文本, 会被 `_expect_tool` 判成 `model_protocol_error`
落 `failed` (SPEC-002 第四节) —— **状态机里没有一条"我拒绝"的合法出口**。
模型自己看出不对时唯一得体的动作是调 `ask_clarification`。所以初稿那个
`model_refusal` 取值是一厢情愿, 它永远不会被记录。

改成六个取值 (SPEC 第一节已给全表):

```
model_clarified        模型自己问回来 —— 最好的一档
model_protocol_error   吐了纯文本没按协议来。算拦住, 但单独一档,
                       报告里要点名它不是好行为 (一次失控, 不是一次得体的拒绝)
schema
static_validator
replay_warning         走到审批人面前了, 只有回放警告提示了问题 —— 不算拦住
none
```

**加一条细则**: 多层都拦过时, 记**"实际终结了这条路的那一层"**, 不是第一次报错
那一层 —— 模型可能先撞 schema 再撞验证器, 决定结局的是后者。这条要配一条测试
(构造一个两层都撞过的轨迹, 断言记的是后者)。

**顺带**: `case_grader` 里凡引用 `model_refusal` 的地方一并改; 枚举与判分表要同源
(与 `missing_slots` 那处一个手法)。

---

## 五、决定 3 补一条负样

同一 `ts_ms` 内排序后比较, 采纳。但要**钉住它没排过头**:

> 负样: 同类型不同参数的两个动作 —— `[notify manager, notify admin]` vs
> `[notify manager, notify operator]`, 排序后**必须仍判不等价**。

你可能已经有了, 有的话在报告里点一下即可; 没有就补。

---

## 六、新增变异 M11

判分口径这种东西一旦悄悄退回去, 没有任何测试会响。所以给第 3 条配一根钉子:

| # | 破坏手法 | 哪条必须红 |
|---|---|---|
| **M11** | 把 `reject` 判据退回"必须命中错误码" | 第 3 条那个 `model_clarified` 正样金样 |

---

## 七、两处留痕

1. **`CHANGELOG.md` 补 v1 定形记录** —— 你自己发现没写的, 由你补最省事,
   补完再交给本人提交。
2. **两个哈希注明是截断的**: `sha256:5bb938673a71eb15` 与
   `sha256:4a91f05807827cac` 都是**截断到 16 个十六进制字符**。用途上够用,
   但不注明的话下一个人拿 `sha256sum` 一比会以为对不上。
   `datasets/README.md` 与 `inventory.json` 旁边各写一句。

---

## 八、重录 cassette: 批准

配置就是你报的那个: 3 条任务 (happy / repair / clarify)、约 7–9 次真实调用、
不到 ¥0.3、`doubao-seed-2-1-pro-260628` / prompt `v3` / `thinking=disabled` /
**`temperature=0`**。20 次调用硬上限保留。

**排在本轮最后一步**, 前面七条全部做完、`make lint` 与全量测试绿之后再跑。理由:
前面几条改的是判分器与金样, 而重录会把三条 ReplayMiss 转绿 —— 现在那三条红灯
是一个干净的对照环境, 别提前毁掉它。

跑完:

- 手编那份 (假 zone 9) 重新手编并重写 `hand_edited` 说明;
- 若录出的响应不带 `missing_slots`, **要改的是 v3 prompt 里对该工具的说明,
  不许手工往 cassette 里补字段**;
- 实际次数与花费追加进 `evals/COST.md` 流水;
- 三条 ReplayMiss 应当转绿, 转不绿要报原因不要绕过。

---

## 九、报告格式

比照前几轮, 本轮追加两节即可 (工作量小, 不用再出配比核对表):

12. **第 3、4 条改完之后, `reject` 类与 `intercepted_at` 的判分在现有 25 条
    illegal / capability_gap / injection 用例上分别落到哪一档** ——
    还没接真模型, 所以这里报的是**金样与构造轨迹上的结果**, 说明这两条新判据
    在各档上都被走到过, 不是只有一条路有测试。
13. **重录的实际次数、tokens 与花费**, 以及 `COST.md` 那一行。

M11 按老规矩报手法与红灯输出; 有任何变异下测试**照样绿**, 原样上报, 不要偷偷
补一条再报绿。

---

## 十、文件边界与老规矩

可动: `evals/` 全部、`apps/api/tests/`、`apps/api/app/services/agent_prompts.py`
(仅限重录时发现 v3 说明要改那一处)、`docs/ai-development/defect-log.md`
(**这是本轮唯一开放的 `docs/` 文件**)、`apps/api/tests/cassettes/`。

不动: `docs/specs/`、`README.md`、`scenarios/`、`packages/policy_engine/`、
`apps/web/`。

- **git 一律不执行**。这一轮刚踩过, 尤其注意: **`checkout` 不算只读**,
  还原文件用 `cp` 备份, 不用 git。
- 临时脚本放 `scripts/dev/`; 密钥只进 `.env`, 不进报告。
- 这一轮不用停, 一口气做完 (重录在最后, 已批准, 不必再等确认)。
