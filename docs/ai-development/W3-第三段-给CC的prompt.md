# W3 第三段 — 给 Claude Code 的 prompt

最后一段：HTTP 接口 + 端到端验收。把分隔线之间的内容复制给 CC。

---

第二段已收尾（172 条测试全绿，三处修补复核通过）。这是 W3 最后一段。

## 依据

- `docs/specs/SPEC-006-policy-lifecycle.md` —— **第五节接口表与第七节验收是本段的依据**
- `docs/specs/SPEC-004-auth-rbac.md` 决策 6 那张权限表 —— 档位以它为准，逐字对齐
- 前两段的 `policy_service` / `policy_runtime` 已定稿，本段基本只在它们上面加一层壳

## 本段范围

做：`app/routers/policies.py`（SPEC-006 第五节那张表里的全部接口）、
`GET /employees`（W2 遗留项）、路由注册、异常到 HTTP 状态码的映射、
以及第七节里需要走 HTTP 才能验的那部分验收。

**不做**：前端（W4）、Agent 相关的一切（W4）。

## 文件边界

- 新建：`app/routers/policies.py`、`app/routers/employees.py`（或合并，你定）、
  对应测试
- 改：`app/main.py`（注册路由）、`mypy.ini`（新模块进严格档白名单）
- **不许碰**：`packages/` 下任何源码、`app/services/policy_service.py`、
  `app/services/policy_runtime.py`、`app/services/incident_service.py`
  （前两段已定稿；**若你认为里面有真 bug，先停下来告诉我，不要直接改**）、
  `alembic/`、`apps/web/`、`docs/`

**不要执行任何 git 命令**。写完列出改了哪些文件 + 建议的 commit message。

## 几处容易踩的地方

1. **权限的真拦截不在路由层。** service 层第二段已经有 RBAC 闸，数据库还有外键兜底。
   路由层这一道是**最外层的快速失败**，不是唯一防线——所以既不要在路由里重写一套
   判断逻辑（会和 service 那套走散），也不要因为"service 会拦"就在路由层不做。
   两层都要有，且都复用同一个权限判定函数。
   这与 SPEC-005 立的"前端隐藏按钮不是安全措施"是同一条道理，只是往下挪了一层。

2. **档位逐字对齐 SPEC-004 决策 6 那张权限表**：`decide` / `publish` / `revoke`
   是 manager+，写草稿 / 校验 / 模拟 / 提交审批是 operator+，读是 viewer+。
   不要自己发挥，也不要顺手"优化"档位——跨文档的权限口径分叉是这个项目反复在修的毛病。

3. **异常映射要给人话。** service 抛的每一类异常都要有明确的状态码，
   数据库约束被违反时抛出来的 `IntegrityError` **不能直接漏给用户**——
   ADR-007 里写了这条分工：数据库保证它不可能发生，应用层保证它发生时人话说得清楚。
   自批 403、已裁决 409、版本状态不对 409、找不到 404、越权 403，
   各配一条测试断言状态码与响应体。

4. **`/docs` 要能交互调用**——这是 W1 就立下的验收项，新接口也算数。
   请求体与响应体都要有明确的 pydantic 模型，不要用裸 dict。

5. **按 W4 需要的形状做**（SPEC-006 第五节已写明用意）。W4 会把这些接口包成
   Agent 工具，其中 `get_available_actions` 那类要返回 JSON Schema 的，
   必须与 `policy_json_schema()` **同一个来源**，不能各生成各的。
   本段先把接口做对，W4 直接封装即可。

6. **第二段在 service 层给事件补上了 `zone_id`。** `SPEC-001` 验收 8 原本写着
   "CSV 回放不覆盖开事故链路"，那条限制现在应该已经解除了。
   请在 `simulate` 接口上**实测确认一遍**：拿 344 条 CSV 跑一条带 `open_incident`
   的策略，看是不是真的有产出了（而不是全落进 `skipped`）。
   **结论告诉我实际数字**，我来决定要不要改 SPEC 那段限制说明。

## 验收

照 SPEC-006 第七节走完整两条主线：

- **主线 A**：operator 写策略 → 静态校验拦住 zone 99 → 改对 → 回放 344 条 →
  提交审批 → operator 自己批吃 403（RBAC）→ manager A 批准 → manager A 发布 →
  跑场景验证策略确实接管了开事故
- **主线 B**：manager A 写策略并提交 → 自己批吃 403（自批规则，与 A 那条 403
  验的不是同一件事）→ manager B 批准 → 发布正常

两条主线**全程走 HTTP**，用种子里的真实账号（`dana@example.com` 是第二个 manager，
`viewer@example.com` 是 viewer）。

## 报告

与前几段相同。另外本段请**自己先做一次变异测试再交给我**：把 `publish` 路由上的
manager 权限校验去掉，确认对应测试真的会红，**报告里贴红灯输出**。
