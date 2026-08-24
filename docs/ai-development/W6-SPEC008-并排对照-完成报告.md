# W6 SPEC-008 并排对照 · 完成报告 (零花费的那一半)

**本轮按覆盖指令只做了不花钱的一半, 真实模型调用一次都没有发生, 花费 ¥0。**
Agent 那份报告在 README 里留了明确标注的占位, 补它的命令与预计花费见第五节。

做了四件事:

```
scripts/dev/render_template_report.py            模板那份: 事实包逐条铺开, 确定性, 不落库
apps/api/tests/test_render_template_report.py    四条测试: 逐字节确定 / 全覆盖按序 / 零落库 / 404
README.md                                        并排对照一节 (模板实文 + Agent 占位) + 进度行 + 已知边界五条
docs/ai-development/本文件                        完成报告
```

对照用的事故也备好了: 开发库 (localhost:5433/sentinel) 的 **事故 #2**, 已解决,
Agent 那步直接对它跑即可。

---

## 一、选了哪条事故、为什么 (含它是怎么来的)

开发库原本一条事故都没有, 所以按任务书"从现有种子/场景里选"的口径, 用现有的
`scenarios/basic_spill.yaml` 场景包**按原速 (x1) 回放**产生了它, 事件一条没改:

- Zone 2 传感器 4 转湿 → 基线策略开单 (normal);
- 30 秒处 Bo Wang 刷卡直接接单 (**没有人派过单** —— 场景包里本来就没有派单事件);
- 75 秒处传感器转干; 随后操作员 alex 经 `POST /incidents/2/resolve` 人工解决。

对照任务书的三条挑选标准逐条核:

1. **有缺失事实**: 未派单 → `assigned_to` 与 `cross_zone` 都是"无此记录";
   人工解决 → `resolve_policy` 也是"无此记录" —— 三条缺失, 对照里看得见
   "缺失也走占位符";
2. **时间线 4 条** (开单/接单/转干/解决), 不触发截断;
3. **人工解决** —— `resolved_by = 'user:3'`, `resolved_kind` = 人工。

两处需要交代的现场操作 (都不是"造事故", 但要写明):

- **基线 wet→open 策略是 SQL 注入的** (policy 9, 含 alex 提交/chris 批准/发布的
  完整审批链, 满足全部数据库约束) —— 开发种子里没有任何开单策略, 现有的那条
  已发布策略是 zone 1 的双探头通知, 开不了单。做法与 `conftest.insert_published_policy`
  逐字相同, 这是搭台子, 事故本身走的是真实 ingest → 引擎 → 状态机路径;
- **事故 #1 是同场景 x10 回放的产物** (先试了一遍演示常用的 `make sim-basic` 节奏),
  时长被压成"2 秒响应", 不忠实于场景包自己声明的 30s/75s 节奏, 所以按 x1 重放得到
  #2。#1 保留未删 —— 它顺带让 #2 的"该传感器近三十天开单数"有了真实的 "2 次"。

## 二、与任务书 (原 prompt) 不一致的地方

- **最大的一条就是覆盖指令本身**: 原任务书的"二、Agent 那一份 (一次真实调用)"
  整段没做, README 对应小节是占位 —— 这是人下的零花费指令, 不是我砍的;
- 原任务书要求报告里写 `ai_usage` 实际花费 —— 本轮零调用, `ai_usage` 里没有本轮
  的任何行, 花费一节改写为"补跑时要查什么" (第五节);
- README 那节的"两个倾向计数实际值"同样属于 Agent 那半, 现在写的是计数的定义与
  "为 0 不能说明什么"的口径 (SPEC 第三节原话), 实际值留在占位清单里;
- 任务书说"README 里不出现'幻觉率'这个词" —— 我连否定式提及 ("本项目不报XX")
  都没写, README 通篇没有这三个字, 用的是 SPEC 允许的说法 (机制 + 计数, 不折比率)。

## 三、新增分支与写操作各由哪条测试守着

| 新增的东西 | 守它的测试 (apps/api/tests/test_render_template_report.py) |
|---|---|
| 确定性 (同一事故两跑逐字节相同) | `test_template_report__byte_identical_across_runs` (subprocess 真跑两次, 比 stdout 字节) |
| 模板的定义: 全覆盖、按序、缺失也铺 | `test_template_report__every_fact_one_line_in_pack_order` (与 `load_fact_pack` 逐行比对, 另断言"派单给: 无此记录。"在场) |
| 不落库 (部分唯一索引那条红线) | `test_template_report__writes_no_incident_reports_row` (跑前后 `incident_reports` 行数都为 0) |
| 事故不存在 → 退出码 1 | `test_template_report__unknown_incident_exits_nonzero` |

写操作: 本轮脚本对库**只读** (走 `report_task_service.load_fact_pack`, 即
service 层, 不自己拼 SQL, CLAUDE.md 不变量 4)。对开发库的一次性写操作
(注入策略 9 / 回放 / resolve) 都是搭现场, 不在任何交付代码路径里。

## 四、报数字带产生它的配置

- README 里那 21 行模板输出: `python scripts/dev/render_template_report.py 2`,
  库 `postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel`,
  时区 `Asia/Shanghai` (`report_task_service.REPORT_TZ`, 代码常量非环境变量);
  "21 行"就是该事故事实包的条目数 (17 条固定 + 4 条时间线, 无截断);
- "27 秒 / 1 分 / 56 秒"这些时长来自 x1 回放的真实墙钟间隔, 与场景包声明的
  30s/75s 节奏一致 (刷卡与转干各有零点几秒的投递延迟, 属实);
- "2 次"(近三十天开单数) 的分母窗口锚在 #2 的开单时刻, 含 #1 与它自己。

## 五、占位在哪、补它要跑什么、预计花多少 (给回来的人)

**占位位置**: `README.md` 的
`## 一次并排对照: 模板报告 vs Agent 报告` → `### Agent 那份` 小节,
以 `**[占位: 待一次真实模型调用后填入。]**` 开头的那段, 连同其下两个列表项
(计数实际值 / ai_usage 花费) 一起替换。

**补跑步骤** (对开发库的事故 #2; 上限 3 次调用, 跑第 3 次前停下来问人):

1. 确认 `.env` 里 key 就位 (不打印), 起 API:
   `PYTHONPATH=apps/api:packages/policy_engine:packages/scenario python -m uvicorn app.main:app --port 8000`
   (从仓库根, venv 内; 或 `make up-bg`);
2. 以 operator 登录, `POST /incidents/2/report` → 202 + task_id;
3. 轮询 `GET /agent-tasks/{task_id}` 到 `awaiting_review`
   (或开 Studio 报告页看时间线);
4. `GET /incidents/2/report` 取 `rendered` 五字段与
   `bare_fact_attempts` / `dangling_ref_attempts`, 填进 README 占位;
5. 查花费 (task_id 换成第 2 步返回的):
   `SELECT model, prompt_version, count(*), sum(input_tokens), sum(output_tokens), sum(estimated_cost_cny) FROM ai_usage WHERE task_id = :task_id GROUP BY model, prompt_version;`
6. README 里 Agent 报告下方标注生成方式 (与模板小节对称), 花费写进同一节或
   完成报告补记。

**预计花费**: 按 SPEC-007 补入 34 的实测口径, 一条报告任务约 3–4 次调用、
¥0.06–0.08; 任务书预估约 ¥0.2, SPEC-008 目标 6 的封顶是 ¥1。本轮实际: **¥0, 零调用**。

## 六、必跑清单退出码 (本轮交付后重跑)

| 命令 | 结果 | 退出码 |
|---|---|---|
| `bash scripts/ci/lint.sh` | ruff + mypy 全过 (161 files, 含新脚本与新测试) | 0 |
| `bash scripts/ci/test-unit.sh` | 299 passed | 0 |
| `make ci-api-repro` (DROP 后全新库, 含 test-api 全量) | 416 passed, 1 skipped (含本轮 4 条新测试) | 0 |
| `bash scripts/ci/test-eval-smoke.sh` | 10 条全过, 零回放 miss, 零注入得逞 | 0 |
| `bash scripts/ci/test-docker.sh` | docker job 全部断言通过 | 0 |
| `python3 scripts/dev/mutate_spec008_stage2.py` | 六条全红 (B1/B1b/B2/B3/B4/B5), 还原后基线绿 | 0 |

(`make ci-api-repro` 是同日上一轮 CI 修复新加的目标, 它替代单独跑 `test-api.sh`,
且条件比 CI 更严 —— 先 DROP 常驻测试库。)

---

## 七、补跑记录: Agent 那半已完成 (2026-08-24)

按第五节六步补跑完毕, README 占位已替换。**1 条报告任务, 2 次真实模型调用**
(上限 3 次, 未触及第 3 次)。本轮只改了 `README.md` 与本文件; 未录制任何东西。

**任务轨迹**: `POST /incidents/2/report` → 202, task_id = 11, report_id = 1;
状态机实际路径 `collecting → drafting → validating → repairing → validating`,
停在 `awaiting_review`, 未 finalize (给演示留着"等人过目"的现场)。
那次 `repairing`: 初稿在 suggestion 里裸写"三十天", 校验器报
`E_BARE_FACT (field=suggestion, detail="三十天")`, 修复稿删掉裸写字样后通过。

### 渲染后五字段原文 (未做任何人工润色)

```
summary:    Zone 2 - 卖场中区 的 4 号传感器 触发水浸告警, 事故编号 #2, 严重级别 一般。
handling:   系统于 2026-08-24 20:24 开单, 由 Bo Wang 接单, 派单对象为 无此记录, 跨区派单情况 无此记录; 最终以 人工 方式关闭, 采用策略 无此记录。
impact:     从开单到解决共 1 分, 响应 27 秒, 到场 56 秒; 同区同期并发未结事故 0 条, 未造成持续影响。
notable:    响应与到场均在极短时间内完成, 传感器在 2026-08-24 20:26 传感器转干 转干后随即于 2026-08-24 20:26 解决 解决。
suggestion: 该传感器 2 次, 建议关注其是否存在偶发误报或轻微渗漏。
```

两处成文瑕疵照实进了 README, 没有修: notable 的"传感器转干 转干后"叠字
(`{{tl_3}}` 占位符文本自带事件名); suggestion 的"该传感器 2 次"丢语境 ——
它正是那次 `E_BARE_FACT` 修复的直接后果: 模型把裸写的"三十天"整个删了,
只留占位符。拦得住裸写数字, 拦不住修完的句子变难读 —— 这句话也写进了 README。

### 倾向计数实际值

- `bare_fact_attempts = 1` (即上述那次 E_BARE_FACT);
- `dangling_ref_attempts = 0`。

### 实际花费 vs 预估

`ai_usage` 按 task_id = 11 聚合 (第五节第 5 步那条 SQL, 库
`localhost:5433/sentinel`):

| 模型 | prompt_version | 调用次数 | input tokens | output tokens | estimated_cost_cny 合计 |
|---|---|---|---|---|---|
| doubao-seed-2-1-pro-260628 | r1 | 2 | 4042 | 497 | ¥0.039162 |

预估是 ¥0.06–0.08 (按 3–4 次调用口径); 实际 **¥0.039**, 低于预估下限 ——
这一跑只经过一轮修复, 2 次调用 (初稿 + 修复稿) 而非 3–4 次。

### 必跑清单退出码 (README 改完后重跑)

| 命令 | 结果 | 退出码 |
|---|---|---|
| `bash scripts/ci/lint.sh` | ruff + mypy 全过 (162 files) | 0 |
| `bash scripts/ci/test-unit.sh` | 299 passed | 0 |
| `make ci-api-repro` (DROP 后全新库) | 416 passed, 1 skipped | 0 |
| `bash scripts/ci/test-eval-smoke.sh` | 10 条全过, 零回放 miss, 零注入得逞 | 0 |
| `bash scripts/ci/test-docker.sh` | docker job 全部断言通过 | 0 |

---

## 八、第二次生成 (2026-08-24, `_fmt_duration` 修复后重跑)

**为什么重跑**: `_fmt_duration` 原来把 83 秒印成 "1 分" (整分截断),
与同一句话里的 "响应 27 秒, 到场 56 秒" 加不平。修复后模板与 Agent 两份的
处理时长都变成 "1 分 23 秒", 第七节那份报告因此作废。

**操作**: 先确认修复过 CI —— `make ci-api-repro` 绿, **418 passed, 1 skipped**
(比第七节多 2 条, 是时长格式的新测试); `POST /reports/1/discard` 弃掉旧报告
(report_id 1 转 discarded, 未删行); 重新 `POST /incidents/2/report` →
task_id = 12, report_id = 2。**本轮 2 次真实调用, 正好顶到 2 次上限, 未超。**
状态机路径与第一跑相同: `collecting → drafting → validating → repairing →
validating → awaiting_review`, 未 finalize。

### 渲染后五字段原文 (第二跑, 未做任何人工润色)

```
summary:    Zone 2 - 卖场中区 的 4 号传感器 触发水浸告警, 事故编号 #2, 严重级别 一般。
handling:   系统自动开单后, 由 Bo Wang 接单并到场处置, 最终以 人工 方式确认传感器转干并关单; 派单对象记录为 无此记录, 是否跨区派单为 无此记录, 解决策略为 无此记录。
impact:     从开单到接单用时 27 秒, 到场用时 56 秒, 全程处理用时 1 分 23 秒; 同区同期并发未结事故 0 条, 未造成持续影响。
notable:    本次响应与处置速度极快, 传感器在接单后极短时间内即转干; 该传感器近 2 次 周期内开单数暂未呈现明显频发趋势。
suggestion: (空字符串)
```

### 倾向计数与本跑的两处瑕疵

- `bare_fact_attempts = 1` (`E_BARE_FACT, field=notable, detail="三十天"`),
  `dangling_ref_attempts = 0` —— **两跑都栽在同一个词上**: "近三十天开单数"
  这个指标的时间窗自带数字, 模型想引用这个指标就容易裸写"三十天";
- 修复的副作用比第一跑更典型: 初稿 notable 本来通顺
  ("该传感器近三十天开单 {{sensor_30d_count}}"), 修复稿把占位符挪进"三十天"
  原来的句位, 得到"该传感器近 2 次 周期内开单数" —— 语法全对、校验全绿、
  句子不是人话, 是"用了对的占位符但放错句子拦不住"的活例, 已写进 README;
- suggestion 两稿都是空字符串 —— 校验器管"写了什么", 不管"该写没写"。
  第一跑的两处瑕疵 (tl 占位符叠字 / suggestion 丢语境) 本跑都没有出现,
  README 里对应段落已按本跑重写, 未保留旧描述。

### 花费 (新跑 + 两跑合计)

| task_id | 模型 | prompt_version | 调用次数 | input | output | estimated_cost_cny |
|---|---|---|---|---|---|---|
| 11 (已弃) | doubao-seed-2-1-pro-260628 | r1 | 2 | 4042 | 497 | ¥0.039162 |
| 12 (现行) | doubao-seed-2-1-pro-260628 | r1 | 2 | 4044 | 484 | ¥0.038784 |
| **合计** | | | **4** | **8086** | **981** | **¥0.077946** |

两跑合计 ¥0.078, 仍在原任务书预估 ¥0.2 与 SPEC-008 封顶 ¥1 之内。
弃掉的 task 11 那 ¥0.039 是 `_fmt_duration` 缺陷的返工成本, 照实记。

**README 同步改动**: 除 `### Agent 那份` 整节按第二跑替换外, 模板小节的
"处理时长 (开单到解决)" 一行也从 "1 分" 改为 "1 分 23 秒" —— 模板脚本与
Agent 共用同一个 `_fmt_duration`, 修复后重跑模板脚本核实过, 其余 20 行逐字未变。
