# SPEC-004 · W2 登录与权限 (JWT + RBAC)

状态: 已评审, 待实现 (W2 待办 3)

## 目标

替换原系统前端硬编码的 `APP_PASSWORD='demo'`, 给系统一个真实的身份层。

不只是"补个登录页"。W3 的版本化审批发布依赖它: `approvals.requested_by` /
`approvals.decided_by` / `policies.created_by` 全部指向 `users`。没有真实身份,
CLAUDE.md 不变量 1 说的"publish 必须存在 approvals 记录"就是可伪造的 ——
而"模型永不直接执行副作用, 必须人工审批"是整个项目的核心主张。
同理 `audit_log.user_id` 目前一直是空的, 审计记了动作没记人。

## 前提: users 与 employees 是两张表 (已定: 方案 A)

`users` (登录账号: email / password_hash) 与 `employees` (现场员工: rfid_uid / zone_id)
在 `0001_initial.sql` 里是**两张互不关联的表**。而 SPEC-003 的 actor 记的是
`employee:{id}`, `audit_log.user_id` 指向的却是 `users.id` —— 这条缝现在被
`X-Actor` 占位符盖住了, JWT 一落地就会暴露。

三种接法:

| 方案 | 做法 | 代价 |
|---|---|---|
| **A (已采纳)** | `users` 加 `employee_id bigint NULL REFERENCES employees(id)` | 一列, 允许账号不绑员工 |
| B | `employees` 加 `user_id bigint NULL REFERENCES users(id)` | 同上, 方向相反 |
| C | 靠 email 匹配 | 不加列, 但两边 email 改一处就断, 且 employees.email 无唯一约束 |

**采纳 A**, 且这一列**可空**, 因为两个方向都真实存在, 种子数据正好覆盖三种情况:
- **有账号、不是现场员工**: `admin@example.com` —— 系统管理员, 不刷卡也不属于任何区域,
  硬塞一条 employees 记录就是编假数据;
- **有账号、也是现场员工**: Chris Li (主管, 管 1 区) 与 Alex Chen (operator),
  账号的 `employee_id` 指向各自的员工记录;
- **是现场员工、没有账号**: Bo Wang —— 只刷卡, 从不登录。硬给他建账号意味着编一个
  永远不会被使用的邮箱和密码, 既是假数据又多一个可被攻击的入口。

若这一列设成必填, 上面第一种和第三种都得靠编数据绕过去。

落地后, actor 的口径统一成: 登录用户操作记 `user:{id}`, 刷卡记 `employee:{id}`,
系统自动动作记 `system` / `auto_sensor_dry`。`audit_log.user_id` 只在前者有值。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/login` | `{email, password}` → 种下 httpOnly cookie, 响应体回 `{user, roles, expires_at}` |
| POST | `/auth/logout` | 清掉 cookie (置空 + 立即过期) |
| GET | `/auth/me` | 当前用户 + 角色列表 + 绑定的 employee (若有) |

登录响应体里**不回 token 原文** —— 回了就等于把"JavaScript 读不到"这个前提自己破坏掉。

**不做**: 注册接口 (用户由种子/管理员创建)、密码重置、OAuth、refresh token。
理由: 这是给面试官看的演示系统, 不是要上线的 SaaS。refresh token 列为 W6 可选。

## 关键决策

1. **密码用 bcrypt**, cost 12。表里 `password_hash` 已建好。
   密码在任何日志、任何响应体里都不出现 —— 有一条测试专门断言这件事。
2. **JWT 用 HS256**, 密钥取 `SENTINEL_JWT_SECRET` (config 里已有 `jwt_secret`, 默认值
   `dev-only-change-me`)。**启动时若在非开发环境仍是默认值, 直接拒绝启动** ——
   宁可起不来, 也不要带着一个公开的签名密钥上线。
3. **token 装在 httpOnly cookie 里**, 属性 `HttpOnly; SameSite=Lax; Path=/`,
   非本地环境再加 `Secure`。
   - 为什么不是 localStorage: 那里任何 JavaScript 都能读, 页面上只要有一处 XSS,
     一行 `localStorage.getItem` 就能把令牌偷走。httpOnly 是浏览器强制的,
     脚本读不到, XSS 偷不走。
   - 为什么不是"存前端内存": 那样每次刷新页面都要重登, 开发和录演示视频时很磨人。
   - CSRF 由 `SameSite=Lax` 兜住: 跨站发起的 POST 浏览器根本不会带上这个 cookie。
     本项目前端与 API 同为 localhost (端口不同不影响 same-site 判定), 正常请求不受影响;
     现有 CORS 已配了明确的来源白名单, 只需再允许携带凭据。
     **不额外做 CSRF token** —— 在 SameSite=Lax 之上再叠一层, 对这个规模的系统是过度设计,
     真要做也应等到 W6 上线、域名确定之后。
   - **同时接受 `Authorization: Bearer`**: cookie 优先, 没有再看请求头。
     理由是 `/docs` 的 Authorize 按钮和 curl 都只会走请求头, 而"`/docs` 可交互调用"
     是 W1 就立下的验收项, 不能因为换了存储方式就废掉。
     两者并存不削弱安全性 —— 攻击者拿不到 cookie, 也就拼不出这个请求头。
4. **有效期 8 小时**, 无刷新。演示与面试场景够用, 过期返回 401。
   cookie 的过期时间与 token 内的 `exp` 保持一致, 不能只靠其中一个 ——
   cookie 过期只是浏览器不再发送, 服务端仍须自己校验 `exp`。
5. **角色用现成的多对多**: `users` / `roles` / `user_roles` 三张表 0001 已建,
   种子已插入 viewer / operator / manager / admin 四个角色。一个用户可有多个角色,
   权限取并集。
6. **权限点** (服务端强制, 不信任前端):

   | 能力 | viewer | operator | manager | admin |
   |---|:---:|:---:|:---:|:---:|
   | 读 `/status/*`、`/incidents` | 是 | 是 | 是 | 是 |
   | `assign` / `acknowledge` / `resolve` | 否 | 是 | 是 | 是 |
   | **跨区派单 `allow_cross_zone=true`** | 否 | 否 | 是 | 是 |
   | W3: 审批发布 policy | 否 | 否 | 是 | 是 |
   | 用户与角色管理 | 否 | 否 | 否 | 是 |

   **跨区派单收归 manager** 是 SPEC-003 决策 7 的自然延伸: 那条决策把跨区从"禁止"
   放宽成"显式放行且留痕", 现在再补上"谁有资格放行"。operator 传
   `allow_cross_zone=true` 返回 **403**, 与"员工不在本区"的 **422** 是两回事,
   不要混用状态码。
7. **`/ingest` 不加鉴权**。它模拟的是设备上报, 真机走的是 MQTT + X.509 证书,
   不是用户会话。加 JWT 会让模拟器与真机行为分叉 —— 与 SPEC-003 决策 6 同一个理由。
   W6 上线时再补设备侧凭据, 记在 ADR。
8. **从 `X-Actor` 切换到真实身份**: `X-Actor` 请求头**整体删除**, 不保留兼容。
   受影响的是 SPEC-003 的 assign / acknowledge / resolve 三个接口与它们的测试;
   `incident_service` 的函数签名不变 (仍收一个 `actor: str`), 只是改由路由层从 token 拼。
   刷卡路径不受影响。

9. **新增模块必须进 mypy 严格档白名单**。`apps/api/app` 下的模块目前已全部纳入
   (见 `mypy.ini`), 新写的 `app.routers.auth` / `app.services.auth_service` 之类
   要一并加进那一行的模块列表, 否则新代码会悄悄退回默认档 ——
   这正是 ADR-005 要防的"声明与执行不一致"。

10. **登录限流按来源 IP, 不按账号**。窗口内失败超过阈值返回 **429** 并带 `Retry-After`。
    - **不按账号锁**: 那本身是个漏洞 —— 攻击者故意去错几次 `chris@example.com`,
      就把真正的 Chris 关在门外了, 限流反过来成了拒绝服务的工具。
    - **只计失败**, 且登录成功后清掉该 IP 的记录: 正常用户敲错两次再登对,
      不该被自己之前的手误拖累; 攻击者的尝试全是失败, 照样被计。
    - **默认不信 `X-Forwarded-For`**: 那是客户端可以随便写的请求头, 直接采信等于
      把限流关掉。只有部署在自己的反向代理后面 (W6) 才打开
      `SENTINEL_TRUST_PROXY_HEADERS`, 且必须由代理**覆盖**而非追加该头。
    - 阈值与窗口是配置项 (默认 10 次 / 300 秒)。
    - **已知边界**: 计数在进程内存里, 单实例够用, 多实例要换 Redis 一类共享存储;
      重启即清零 —— 影响有限, bcrypt cost 12 本身已把每次尝试压到约 270ms。

11. **两条登录失败路径的耗时必须接近**。"不区分邮箱不存在与密码不对"只做到一半是
    没用的: 若邮箱不存在时短路掉 bcrypt, 响应会快上百倍, 攻击者靠计时即可枚举出
    哪些邮箱注册过。邮箱不存在时也要拿一个预置的假哈希跑一次 bcrypt 做无用功。

## 数据库

一个 Alembic 迁移 (`make migrate-new id=0005_users_employee_link m="..."`):

- `users.employee_id bigint NULL REFERENCES employees(id)` (决策见上);
- **不给 `employees.email` 加唯一约束** —— 关联走 `employee_id` 外键, 不靠 email 对齐
  (方案 C 被否掉的理由);
- 种子数据补三个账号 (dev seed, `SENTINEL_APPLY_DEV_SEED=true` 时写入):
  `admin@example.com` (admin)、`chris@example.com` (manager, 绑 Chris Li)、
  `alex@example.com` (operator, 绑 Alex Chen)。
  **密码写在 `.env.example` 与 README**, 面试官一键复现时要能直接登进去。

## 验收

- 未登录访问 `/incidents` 返回 401; 带过期 token 返回 401;
- 用种子账号登录后 `/auth/me` 返回该用户与角色; **刷新页面不需要重新登录**;
- 登录响应的 `Set-Cookie` 带 `HttpOnly` 与 `SameSite=Lax`, 且**响应体里没有 token 原文**;
- `/auth/logout` 之后再访问 `/incidents` 返回 401;
- 用 `Authorization: Bearer` 也能通过鉴权 (保证 `/docs` 与 curl 可用);
- viewer 调 `/incidents/{id}/assign` 返回 403;
- operator 带 `allow_cross_zone=true` 返回 **403**; manager 同样请求成功;
- 给不同区的员工派单且不带 flag, manager 也返回 **422** (权限与业务校验是两层);
- 登录后做一次 assign, `audit_log.user_id` 有值且等于该用户 id;
- 响应体与日志中不出现明文密码 (专门一条测试);
- 同一 IP 连续失败到阈值返回 429 且带 `Retry-After`; 换一个 IP 不受影响;
- 伪造 `X-Forwarded-For` 不能绕过限流 (默认不信任该头);
- 登录成功后该 IP 的失败计数被清空;
- "邮箱不存在"与"密码不对"两种失败的耗时在同一量级 (计时侧信道);
- `SENTINEL_JWT_SECRET` 仍为默认值且非开发环境时, 应用拒绝启动;
- SPEC-003 原有测试全部改造完成, `X-Actor` 在代码与测试中零残留 (用 grep 断言);
- 测试命名遵循 `test_<行为>__<条件>`; `make lint` 与全部测试绿。

## 不在本 SPEC 范围

React 登录页与前端会话处理 (SPEC-005, 前端不再需要自己存 token)、设备侧凭据 (W6)、
refresh token / 单点登录 / 密码策略 (不做)。
