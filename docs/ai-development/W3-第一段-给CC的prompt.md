# W3 第一段 — 给 Claude Code 的 prompt

把分隔线之间的内容复制给 CC。

原则：**这份 prompt 只写 SPEC 里没有的东西**（文件边界、报告格式、易错点指路），
不复述 SPEC 内容——抄一遍就等于同一个事实存两份，改了 SPEC 这里不跟着改就走散了。

---

我们做 sentinel-ops 的 W3 第一段。

## 依据

- `docs/specs/SPEC-001-policy-dsl.md`（2026-08-07 整篇重写）——**本段的唯一依据，逐条实现**
- `docs/specs/SPEC-006-policy-lifecycle.md`——通读一遍即可，本段不实现它，
  但你要知道引擎产出的 Effect 后面被谁消费
- `docs/进度与交接.md`——只看末尾的「W3 进度」一节，前面的历史不用读

## 文件边界（我在并行改文档，别撞车）

**只动 `packages/policy_engine/` 与 `scenarios/`。**

- 新建：`policy_engine/replay.py`、`scenarios/auto_close.yaml`
- 重写：`dsl.py`、`engine.py`、`validator.py`、`__init__.py`、`tests/` 下的测试
- **只改注释、不动 events**：`scenarios/multi_sensor_escalation.yaml`
  （头部注释里的 `zone_manager` 与 `t≈125s` 都已被本次定稿推翻，正确值见 SPEC-001；
  那些 events 被别的测试依赖着）
- 需要的话可以改 `mypy.ini` 里严格档白名单那一行，把 `policy_engine.replay` 加进去

**不许碰**：`apps/` 下任何文件、`alembic/`、`migrations/`、`packages/scenario/` 的源码、
`scenarios/basic_spill.yaml`。数据库与接口全部是第二段的事。

一个例外：**`packages/scenario` 不许改源码，但可以 import 它**。
测试里读 CSV 和 YAML 都调它的 loader（那个包允许读文件），
读出来的事件列表再交给 `replay.py`——`policy_engine` 是零 IO 包，这条边界不要破。

## 不要执行任何 git 命令

`CLAUDE.md` 里写了，这里再强调一次。写完列出「改了哪些文件 + 建议的 commit message」，
由我在终端敲。

## SPEC 里已经写死、不要自行发挥的六处

动手前请把对应段落再读一遍。这几处都是评审时反复推敲过的，
看起来有更"自然"的写法，但那些写法会让验收失效：

1. **`wet_sensor_count` 的语义**（第二节）——两种读法在 t=200s 结论相反
2. **cooldown 的键与边沿状态的键是两个不同的键**（第二节末 + 第四节）——
   合并成一个会让验收 6 直接失效
3. **上下文的三态判定**：必定提供 / 条件性提供 / 两列都没有（第二节）
4. **冷却抑制的是产出 Effect，不是跳过判断**（第四节）
5. **事故投影器**（第六节）——本段最容易整块漏掉的东西，
   没有它验收 5、6、7 都跑不起来
6. **回放只出警告、不出拒绝**（第六节）——硬性规定，不要自行加强成拦截

## 验收

照 SPEC-001 第七节那 11 条逐条落成测试，命名遵循 `test_<行为>__<条件>`。
包内既有的零 IO 边界断言测试必须继续通过。

## 完成后给我一份报告，包含

1. 逐个文件说明改了什么（不要概括成一句话）
2. 建议的 commit message
3. `make lint` 与测试的**实际输出**——贴命令和结果，不要只写「通过」
4. **SPEC 里写了但你没做的、或做法与 SPEC 不一致的地方，逐条列出并说明原因。**
   这条别漏。宁可说「这里我偏离了 SPEC，因为……」，也不要默默改掉。
5. 你在实现过程中发现的 SPEC 本身的问题（有就说，没有就说没有）

---

## 给我自己的复核备忘（不要发给 CC）

报告到手后贴回对话，逐行看代码，不只看报告。前两周有两次报告和实际代码对不上，
都是看代码才发现的。本段重点看六处：

1. `wet_sensor_count` 挑的是哪种读法——去看 t=200s 那条测试真的断言了 2 没有
2. cooldown 的键和边沿状态的键**是不是两个**。这是最可能出的错，
   而且如果 CC 顺手把验收 6 的测试也改软了，光看测试绿是发现不了的——
   **要看测试本身断言了什么**
3. tick 边沿触发有没有做成「每个 tick 都触发」
4. `replay.py` 有没有偷偷把警告做成拦截
5. 事故投影器里 `open_incident` 的空操作有没有实现。没实现的话模拟会比线上多开事故，
   而且测试可能碰巧还是绿的
6. `roles_present` 查的是 `user_roles` 还是 `employees.role`
