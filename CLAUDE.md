# Sentinel — AI Coding 约定

## 架构不变量 (违反即 review 打回)
1. LLM 永远没有副作用执行权限: 只能产出 Policy 草案/报告草稿; publish 必须存在 approvals 记录 (DB 层校验)。
2. packages/policy_engine 零 IO: 纯函数吃事件流。执行器与模拟器是同一份 evaluate()。
3. DSL 的能力边界 == 模拟器可验证边界。加能力必须同时提交 schema + 语义校验 + 引擎实现 + 场景包用例。
4. 只有 services/ 层可以碰数据库; Agent tools 与 MCP server 复用同一 service。
5. requires_approval 由服务端从 action 分级推导, 永不信任模型输入。

## 研发流程 (Spec Coding)
- 功能先写 docs/specs/SPEC-xxx.md (半页: 目标/接口/验收), commit message 关联 spec 编号。
- 关键取舍写 docs/adr/, 每篇 ≤1 页。
- AI 生成代码引入的缺陷, 在 docs/ai-development/defect-log.md 记录完整案例。

## 工程约定
- Python 3.12 / ruff / mypy; TS strict。
- mypy 分档: 应用与引擎代码开严格档, 测试默认档; 严格档逐模块打开,
  白名单在 mypy.ini, 新模块补完标注后加入。理由与待补清单见 ADR-005。
- 测试命名: test_<行为>__<条件>。评测 grader 必须确定性, 禁止 LLM judge。
- 文档只用文字与 Markdown 结构表达状态 (完成 / 进行中 / 未开始), 强调用粗体;
  不用表情符号与装饰性图标。

## 协作红线

- **git 一律由本人执行**。AI 可以直接写文件、改代码, 但 `git add` / `git commit` /
  `git push` / `git reset` / 任何改动仓库历史的命令, 都由本人在自己终端敲。
  AI 只负责列出"这批改了哪些文件、建议的 commit message", 不代劳执行。
  (背景: 通过文件桥接跑 git 会在 `.git/` 留下 `index.lock` 残留, 导致后续 git 报
  `Unable to create '.git/index.lock': File exists`。)
- 多个 AI 同时改同一个仓库时, **先划清文件边界再动手**。文件桥接是整文件写入,
  没有合并, 谁后写谁赢 —— 覆盖前先比对校验和, 确认对方没在你取快照之后动过。
