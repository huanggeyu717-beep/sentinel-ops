# SPEC-002 · W4 Agent 编排状态机

状态: 已定稿 (2026-08-07, W4 开工前评审重写)。原版是 W1 之前的草稿 (839 字节),
W3 开工时记了五条待修订, 本次连同整篇一起补齐。文末列出**仍需拍板的四处**。

## 目标

把管理员的一句人话 ("生鲜区两个探头三分钟内都湿了就通知这个区的主管") 变成一条
**已通过静态校验、已在历史数据上回放过、正在等人审批**的 Policy 草案。

Agent 只做三件事: 理解、编译、把校验与模拟的结果摆到人面前。
**它没有任何副作用执行权限** —— 发布那一步由人点, 且被数据库外键强制
(`CLAUDE.md` 不变量 1 / ADR-007)。

W3 已经把它需要的一切都做好了: DSL 是封闭白名单 (SPEC-001)、双层验证器给结构化错误码、
生命周期接口按本 SPEC 需要的形状实现 (SPEC-006 第五节)。W4 基本是在这些之上包一层。

## 非目标

- **自由代码执行、让模型直接调数据库**: 与整个设计前提冲突。
- **多轮对话式修改**: v1 是"一句话 → 一条草案", 不做会话记忆。改策略 = 再说一句话,
  Agent 读现有版本再产出新版本。
- **跨策略编排**: 模型一次只处理一条策略。
- **自动发布**: 永远不做。这是项目的核心主张。

---

## 一、状态机

```
                                  ┌──────────────────────────────┐
                                  ▼                              │
  parsing → discovering → compiling → validating ──(有错误)──→ repairing
                                         │                       │
                                    (通过) │                  (修够 2 次仍不过)
                                         ▼                       │
                                    simulating                   ▼
                                         │                   clarifying
                                         ▼                       │
                                  awaiting_approval              │
                                         │                       ▼
                            (人在 Studio 里批准并发布)         failed
                                         ▼
                                     completed
```

- `parsing` 读懂说的是什么; `discovering` 调只读工具把区、传感器、角色捞回来;
  `compiling` 产出 Policy 草案; `validating` 调静态验证器; `simulating` 调回放。
- **`repairing` 修完必须回到 `validating` 重新校验**, 不是直接去 `simulating`。
  原草案图上这条边是断的 —— 修完不验等于没修。
- **修满 2 次仍不通过 → `clarifying`, 不是直接 `failed`。** 连续两轮都改不对,
  说明多半不是模型手滑而是需求本身有歧义 (比如"主管"指哪个角色), 该回头问人。
  人不回应或回应后仍不通过, 才落 `failed`。原草案在这里只有一条出边, 没有答案。
- `rejected` 不在状态机里: 审批被拒是**策略版本**的状态 (SPEC-006 第二节),
  不是 Agent 任务的状态。任务在提交审批那一刻就 `awaiting_approval` → 人处理完即
  `completed`, 批没批通过与 Agent 无关。
- 每次状态迁移写一条 `agent_steps`, 并经 SSE 推给前端 (见第五节)。

---

## 二、工具清单 (11 个)

原草案锁 9 个, 本次补 2 个。分级依据是 `CLAUDE.md` 不变量 1。

| 类别 | 工具 | 说明 |
|---|---|---|
| 只读 | `list_zones` | |
| 只读 | `list_sensors` | 带 zone 归属, 静态验证要用 |
| 只读 | `list_roles` | 取值域是 `roles` 表 (SPEC-001 第五节定死的) |
| 只读 | `list_employees` | **本次新增**, 包 W3 补的 `GET /employees` |
| 只读 | `get_policy` | **本次新增**, 读现有策略与版本 —— 没有它, Agent 只能新建, 改不了已有策略 |
| 只读 | `get_available_actions` | 返回 Policy 的 JSON Schema |
| 草案 | `create_policy_draft` | 写 `policy_versions`, 状态 draft |
| 模拟 | `validate_policy` | 静态校验, 返回结构化错误码 |
| 模拟 | `simulate_policy` | 历史回放, 返回 `ReplayReport` |
| 写 | `request_approval` | 建 approvals 记录, 版本转 awaiting_approval |
| 终止 | `ask_clarification` | 把问题抛回给人 |

**`publish_policy` 不在工具清单里。** 原草案有它, 本次删除 —— 发布是人在
Automation Studio 里点的动作, 不该出现在 Agent 的能力范围内。少一个工具,
就少一处需要论证"为什么它不会乱用"。

**`get_available_actions` 返回的 Schema 必须与 `policy_json_schema()` 同一个来源。**
W3 已经做到了 (请求体直接用 `policy_engine.Policy` 模型, OpenAPI 里暴露的就是它),
W4 直接 import 即可, 不要另生成一份 —— 两份 Schema 一定会走散, 而走散的那份
从外面看不出来 (W1 教训)。

### 权限与拦截: 四层, 只有前三层是安全措施

| 层 | 干什么 | 是不是安全措施 |
|---|---|---|
| 数据库 | `policy_publications.approval_id` NOT NULL 外键 | **是**, 最终防线 |
| service | RBAC 闸 + 自批 CHECK (W3 已实现) | **是** |
| 路由 | `require_permission` 最外层快速失败 | **是** |
| 工具清单 | 按调用者角色裁剪能给模型的工具 | **不是**, 只是减少模型做无用功 |

**原草案的验收写着"越权 → 工具列表内无 `publish_policy`", 这是错的, 而且是退步。**
SPEC-005 已经立过一条: "前端按角色隐藏按钮不是安全措施, 真正的拦截在服务端。"
同一个道理 —— 不把工具给模型也不是安全措施, 因为模型的输出经过的是同一批接口。
定稿把两件事分开写: 裁剪工具是优化, 拦截在下面三层, 且各有测试。

---

## 三、预算与恢复

| 项 | 值 | 来源 |
|---|---|---|
| 全局 | 120 秒 | 待实测回填 |
| 单工具 | 10 秒 | 待实测回填, `simulate_policy` 很可能不够 |
| 修复次数 | ≤ 2 | 定死 |
| 退避 | 仅对可重试错误指数退避 | 定死 |

**`simulate_policy` 的 10 秒是拍的, 必须实测。** W3 第三段实测过一次回放:
344 条真实读数 (装载后 1258 条事件) 走 HTTP `simulate` 接口跑通, 但没有计时。
W4 开工第一件事就是量一次, 把真实数字填进这张表, 并按最慢的那次留余量。

**超限的处理**: 落 `agent_tasks.status = 'dead_letter'`, 并写明原因。
`agent_tasks` 现在只有 `error_code` 一列, 没有地方放人话解释 —— 本 SPEC 要求
**迁移 `0008` 给它加一列 `error_detail text`**。理由: 死信的解释是任务级的事实,
塞进某一条 `agent_steps.result_summary` 会让"这个任务为什么死了"变成要翻步骤才知道。

---

## 四、LLM 接入与可复现

- 走火山方舟 (豆包), OpenAI 兼容协议, key 在 `.env` 的 `SENTINEL_LLM_API_KEY`。
- **每次调用落 `ai_usage`**: 模型、prompt 版本、输入输出 token、预估成本、延迟、是否命中缓存。
  W5 的消融实验要靠这张表算成本。
- **录制回放**: 调用与响应写进 `SENTINEL_LLM_RECORD_REPLAY_DIR` (默认 `.llm-cache`)。
  W5 的评测必须能离线复跑, 否则每跑一次评测集都要真花钱、且结果不可复现。
- **prompt 版本号进 `ai_usage.prompt_version`**, 改 prompt 就换号 ——
  否则消融实验里"这次好是因为改了 prompt 还是换了模型"分不清。

---

## 五、Trace 与推送

- `agent_steps` 是 Trace UI 的**唯一事实源**: 每步记工具名、参数、结果摘要、状态、
  延迟、重试次数、token 数。
- **用 SSE 不用轮询**。这与 SPEC-005 的判断不冲突 —— 那里状态与事故用 5 秒轮询,
  是因为它们是"看当前值"; Agent 执行是"一步步冒出来"的过程, 轮询会丢中间步骤,
  而中间步骤正是这个功能要展示的东西。`sse-starlette` 在 W2 就是为此留的。
- 断线重连从 `agent_steps` 的 `seq` 续传, 不从头推。

---

## 六、验收

1. **正常路径**: 一句人话 → 产出草案 → 静态校验通过 → 回放出报告 → 提交审批,
   全过程在 Trace UI 上逐步冒出来。
2. **修复循环**: 故意让模型产出一个引用了不存在 zone 的草案 → 静态验证器返回
   `E_UNKNOWN_ZONE` → Agent 仅凭错误码与 hint 修对 → 重新 `validating` 通过。
   **这条要验的是"错误码够不够修复用"**, 是 SPEC-001 第五节那套错误码的真实检验。
3. **歧义 → clarifying**: 说"漏水了通知一下" (没说通知谁、几个探头) → Agent 问回来,
   不猜。
4. **修满两次仍不过 → clarifying 而不是 failed**。
5. **越权**: operator 的会话跑 Agent, 产出的草案能提交审批但发布按钮是灰的;
   **绕过前端直接调发布接口仍然 403** —— 这条才是安全测试, 工具裁剪那条不是。
6. **工具超时**: 注入一次超时 → 可重试错误退避后成功; 不可重试错误干净失败并落
   `dead_letter`, `error_detail` 里有人话。
7. **每步落 `agent_steps` 并 SSE 推送**; 断线重连从 `seq` 续传不重复。
8. **离线复跑**: 关掉网络, 用 `.llm-cache` 里的录制把同一条任务原样跑一遍, 结果一致。
9. `make lint` 与全部测试绿; 测试命名遵循 `test_<行为>__<条件>`。

## 不在本 SPEC 范围

Automation Studio 的前端页面 (SPEC-005 延续)、评测集与消融 (W5)、
事故报告生成 Agent (W5)、MCP server (W6)。

---

## 需要拍板的四处 (我没有替你决定)

1. **Agent 产出的草案挂在谁名下?** `policy_versions.created_by` 指向 `users`。
   记发起对话的那个人, 还是记一个专门的 "agent" 系统账号? 前者让审批时看得见
   "是谁让 Agent 写的", 后者让"人写的"与"AI 写的"可以分开统计 (W5 可能要这个数)。
   也可以两个都要: `created_by` 记人, 另加一列标记来源。

2. **`agent_tasks.idempotency_key` 怎么生成?** 表里有这一列 (0001 就建了) 但没定义。
   用"用户 + 输入文本的哈希"的话, 同一个人把同一句话说两遍会被当成重试而不是新任务 ——
   这是想要的行为还是不想要的?

3. **失败的任务要不要留草案?** Agent 跑到一半失败, 已经写进 `policy_versions` 的
   draft 版本是删掉还是留着? 留着可以让人接手改, 但会在版本列表里堆一堆半成品。

4. **超时预算实测之后, 如果 `simulate_policy` 真的要几十秒怎么办?**
   拉长单工具预算, 还是让模拟异步跑 (Agent 先提交、结果稍后推)?
   后者更接近真实产品形态但会让状态机多一个等待态。建议等实测数字出来再定,
   这里先记着。
