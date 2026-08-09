# W4 第三段 给 CC 的 prompt

第二段补录已复核通过：六处修补全落到位，五个 cassette 入库，思考开关那个两臂探针
是这一段最值钱的产出（已写进 SPEC-002 第八节）。复核发现一处要改、一处要补，
列在下面开工前的两件小事里。

本文件只写 SPEC 里没有的东西。依据是 `docs/specs/SPEC-002-agent-orchestration.md`，
**该文档又更新过**（第八节新增"深度思考开关"整节 + 120 秒实测回填、第三节 60 秒
那一格注明与思考开关绑定、第九节回放键字段补上思考开关与"阶段名不进键"）。
**开工前重读第一、三、八、九、十节与第十二节验收。**

---

## 开工前两件小事

**一、`CANONICAL_INVENTORY` 的 roles 少了 `viewer`。**

你报告里写"roles 加 viewer"，代码里是 `["admin", "manager", "operator"]`，
注释还写着"纯 dev seed，不含 viewer"——**报告与代码相反，而且代码是错的。**

评审方灌了一个库跑过：`0001` 建表 + `db.py` 的 dev seed + **迁移 `0007` 的种子**，
`inventory_service._ROLES_PRESENT` 的原文查询返回 **admin / manager / operator /
viewer 四个**。

根因：`0007` 自己也种了 `dana` 和 `viewer`（SPEC-006 第三节要求的，为了补 SPEC-005
那个"viewer 只在夹具里验过"的遗留项），"dev seed"不等于 `db.py` 那一段。
误导你的是 `conftest.viewer_headers` 的 docstring "**种子只有另外三种角色**"——
**那句话在 `0007` 之后就过期了**，那个夹具的 `ON CONFLICT DO NOTHING` 现在实际上
什么都没插。

要做三件事：`CANONICAL_INVENTORY` 的 roles 补回 `viewer`；**把 conftest 那句过期
docstring 改掉**（不然下一个人还会踩）；**五个 cassette 因为 prompt 变了要重录**。

现在重录约两毛钱；等 W5 评测集建起来再改，作废的就不止五个了——**所以这件事必须在
第三段之前做完，不能拖。**

**二、澄清路径的真实录制被删了，补录一条。**

含糊版 `REPAIR_INPUT` 那次录制证明了真模型在真实歧义面前**确实会主动问人**，
而且问的时候自己指出了"sensor id=0 从未上报过数据，将不纳入监控"——修补五在真模型
行为里当场兑现。那两个 cassette 被删了，结果是三条主路径里**澄清那条只剩打桩覆盖，
没有任何真模型证据**，而第二段的目的恰恰是用真模型验证这套东西成不成立。

重录一条含糊输入的 cassette（约 ¥0.05），挂一条 `@needs_cassettes` 的**验收 3**
（歧义 → `clarifying`，模型问回来而不猜）。

改 `REPAIR_INPUT` 那个判断本身是对的，两层断言一字未动，不用回退。

---

## 第三段要做什么

SPEC-002 分段实施的第三段：**HTTP 路由 + SSE + Automation Studio 前端 + Trace UI**。
做完就是 W4 结束，也是本项目的**最早可投递点**（要录第一版视频）。

## 文件边界

**归你：**

- `apps/api/app/routers/agent_tasks.py`（新建）、`apps/api/app/main.py`（注册路由 +
  后台任务集合的关停）
- `apps/api/app/services/agent_runtime.py`（只加后台 spawn 入口，状态机不动）
- `apps/api/tests/test_agent_http.py`（新建）、已有的 `test_agent_*.py`
- `apps/web/src/features/studio/`（新建目录，Automation Studio 与 Trace UI）
- `apps/web/src/api/{client,queries,types}.ts`、`App.tsx`（加路由）
- `apps/api/tests/cassettes/`（重录，见上）、`apps/api/tests/test_agent_llm.py`
- `mypy.ini`

**冻结，一律不动**：`packages/**`、`docs/**`、`.env`、迁移、`policy_service.py`、
`agent_service.py`、`agent_tools.py`、`agent_prompts.py`、`llm_client.py`
（除非发现真 bug —— 那就**先停下来说**，不要擅自改）。

`apps/web` 那边沿用已有的结构与写法（`features/dashboard/` 是现成的参照）。

## 易错点指路

**一、后台任务的引用必须留住。**

`asyncio.create_task()` 返回的对象如果没有强引用，**可能在跑完之前被垃圾回收掉**，
表现是任务随机消失、日志里什么都没有。标准做法是存进一个模块级 `set`，
并在 task 的 `done_callback` 里 `discard`。关停时连同 tick、maintenance 两个循环
一起干净取消（`main.py` 里已有那个 `for task in (...)` 的形状，扩一下即可）。

**二、同时在跑的后台任务要有上界。**

SPEC-005 决策 4 立过"内存里的东西必须有上界"，演练历史照此只留最近 N 次。
这里同理：一个人可以同时提交多条不同输入的任务，每条都是一个后台协程 + 若干次
真实模型调用。给并发在跑的任务数一个上界（建议 4），超出返回 **429** 并说明。
上界值进 config，就地注释写明它管的是什么。

**三、SSE 的数据从哪来：读数据库，不要建内存事件总线。**

Trace 的事实源是 `agent_steps` + `agent_clarifications`（SPEC-002 第十节），
SSE 处理器按 `seq` 尾随查询这两张表即可。**不要为了"实时"另建一个内存里的
发布订阅**——那会让"事实源只有一个"这件事作废，而且任务与 SSE 连接一旦不在同一个
进程（多 worker、W6 多实例）就直接失效。数据库尾随慢一点，但它永远是对的。

SPEC-005 说"状态与事故用轮询、Agent 用 SSE"讲的是**前端**怎么拿数据；
服务端内部用什么实现不受那句话约束。

**四、`Last-Event-ID` 的语义要写对。**

`seq` 是**每个任务自己**的编号，不是全局的。SSE 的 `id:` 字段就发 `seq`；
浏览器重连时会自动带 `Last-Event-ID` 请求头，从那个 `seq` **之后**接着推。
断线重连不重复不遗漏是 SPEC-002 验收 18，要有测试。

客户端断开时生成器必须能退出（`sse-starlette` 的 `EventSourceResponse` +
`request.is_disconnected()`），否则连接泄漏、测试挂住。

**五、前端的 SSE 必须走 vite 代理的同源路径。**

浏览器原生 `EventSource` **不支持自定义请求头**，所以 `Authorization: Bearer`
这条路走不通——只能靠 cookie。而你们的会话 cookie 是 `SameSite=Lax`，
**跨源的子资源请求不会带它**。

所以：SSE 的地址必须是 `/api/agent-tasks/{id}/events`（走 `vite.config.ts` 里那个
代理，同源），**不能直连 `http://localhost:8000/...`**。直连的表现是 401，
而且非常难查——看起来像登录状态丢了。生产走 nginx 同源，没有这个问题。

顺带：`EventSource` 需要 `{ withCredentials: true }`。

**六、异常到状态码的映射。**

你在第一段末尾已经备好了：`NotTaskOwner` → 403、`TransitionConflict` → 409、
去重撞索引 → 200 带 `suspected_interrupted` 标记。**去重那条不能返回 4xx**——
SPEC-002 第二节写过为什么：用户明明什么都没等到，报"重复提交"会让人一头雾水。

**七、HTTP 测试用打桩模型，不要走 cassette。**

路由层要验的是"立刻返回 / 权限 / 状态码 / SSE 续传"，与模型输出无关。
用打桩客户端（还能精确控制耗时）。验收 2（POST 立刻返回）就靠打桩故意慢一拍：
**断言 POST 的响应时间远小于任务完成时间**，别用真模型测这个。

**八、第一、二段挂账的验收在这一段结清。**

SPEC-002 验收 2、16、18 与变异 21 都要在这一段落地。变异 21 有个坑，
W3 踩过并写在验收里：拆掉发布路由的 manager 门之后**状态码测不出区别**
（service 层第二道闸照样 403）。要靠"路由门在碰数据库之前就拒绝"来钉——
给一个不存在的版本号，有门是 403、没门会先查库变成 404。

## 前端：Automation Studio 与 Trace UI

沿用 `features/dashboard/` 已有的写法与 `theme.ts`。要有的几块：

- **提交框**：一句人话 + 可选的"改哪条已有策略"，提交后立刻拿到 `task_id` 并开始
  逐步显示；
- **Trace 时间线**：按 `seq` 排，四种 `kind`（`transition` / `step` /
  `clarification_question` / `clarification_answer`）**在视觉上要分得开**。
  每步显示工具名、耗时、token 数。这是这个功能的招牌，值得多花点力气；
- **澄清的回答框**：任务停在 `clarifying` 时出现，回答后时间线接着往下长
  （同一个 `task_id`，不是新任务）；
- **审批区**：回放报告 + 提交审批 + 批准/发布按钮（按角色灰掉，
  **但前端隐藏不是安全措施，这句注释照 SPEC-005 的先例写进代码**）。

**回放报告里的警告要显眼，尤其是 `W_NEVER_TRIGGERED`。**

招牌句编出来的策略在 344 条历史数据上一次都不触发（生鲜区没出现过 180 秒内双探头
齐湿）。**这不是缺陷，是回放该如实告诉审批人的东西**，但它必须在界面上被看见、
而且要有一句人话解释（"这条规则在这段历史数据里一次都没命中——可能是条件写紧了，
也可能是这种情况本来就没发生过"）。

理由：录视频时这一幕是个**卖点而不是短板**——系统没有假装自己有用，
它把数字摆在人面前，让人来判断。这正是人工审批那一关存在的意义。
界面上要给这句话留位置。

## 完成报告

沿用八节格式。这次特别要有的：

1. **重录后的 cassette 清单**：几个、哪个是手编的、这轮真实调用几次、花了多少钱
   （接着上一轮的总账记，W5 的基线从那里起）；
2. **端到端截图或录屏说明**：一句人话 → Trace 逐步冒出来 → 报告 → 审批。
   这一段的产出是给人看的，文字描述不够；
3. **SSE 断线重连的实测**：断开再连，从 `seq` 续传，不重复不遗漏的原始输出；
4. **验收 2 的实测数字**：POST 的响应耗时 vs 任务完成耗时，带打桩的延迟配置；
5. **变异 21 的结果**：拆掉路由门之后红的是哪一条、用的是不是"不存在的版本号"
   那个手法；
6. **自行新增的分支/写操作，逐条说明它由哪条测试守着**（固定要有的那一节）。

## 不要做的事

- 不改 `docs/`、不动 `.env`、不执行任何 git 命令；
- 不动迁移与 `ai_usage` 列名（留给 W5）；
- 不为了让界面好看去改后端已经定稿的行为——界面要迁就事实，不是反过来。
