# W6 第二段 · 给 CC 的 prompt

> 第一段复核通过, 报告与代码一致, 十条"与 SPEC 不一致"我逐条看过, **全部成立**,
> SPEC-009 已按其中三条改了原文 (通行证判据、配额数字、迁移多一列)。
>
> 这一段两件事: **先补一处钱的正确性问题** (第 0 件, 是 SPEC 的漏, 不是实现的错),
> 然后做第二段本体 —— compose 覆盖层 / Caddy / 生产种子 / web 镜像 / CI docker job /
> 1 GB 内存验收。仍然是**纯本机, 零真实模型调用, 零花费**。

---

## 第 0 件: 回补要挂在"任务进终态"上, 不是"某一轮的结局"上

复核时用 grep 确认过: `budget_service` 全仓只被 `routers/agent_tasks.py` 引用,
**清扫循环一次都没调过回补**。于是停在 `clarifying` 被清扫判死的任务
(`agent_task_ttl_hours` 默认 24 小时), 预扣当天再也回不来。

你在报告第一节第 6 条把这条列成了"刻意保守的边界", 方向没错, **但数字让它不再是
边界而是主路**: 预扣 ¥0.60 × 单账号配额 5 = ¥3.00, **正好等于全站日预算** ——
一个访客点五下、每次看到 Agent 反问就关掉页面, 当天的演示额度就锁死到 UTC 零点。
而"点开、看到反问、关掉"恰恰是公开演示里最常见的一条路径 (README 里那张
`studio-clarify.png` 就是在演示这个功能)。

**SPEC-009 已改, 按新的来做**:

1. **迁移 0010 加第 4 件**: `agent_tasks.hold_refunded_at timestamptz`。
   **直接改 0010, 不要新开 0011** —— 0010 还没提交, 没有任何库跑过它;
2. **回补变成幂等的结算**: 条件更新 `... WHERE id = :id AND hold_refunded_at IS NULL`,
   受影响行数为 0 就不回补。手法与 SPEC-003 状态机推进一致 (条件更新 + 数行数);
3. **任何把任务写进终态的地方都结算一次**: 现在轮次收尾那两处照旧, **再加上清扫**
   (失联判死与 clarifying 超时判死)。这次 `agent_runtime.py` **在可写清单里**;
4. **`config.py` 的 `agent_user_daily_tasks` 从 5 改成 3**。

**必须配的测试, 两个方向缺一不可**:

- 停在 `clarifying` 的任务被清扫判死之后, **预扣回到了台账** (这是新行为);
- **同一笔钱不会被减两遍**: 让轮次收尾与清扫都对同一条任务结算一次,
  断言台账只减一次。没有这一条, 一个"每次都回补"的实现也能让上一条绿 ——
  而它会把钱越算越少, 直到台账见底、护栏形同虚设。

---

## 第二段本体

依据 SPEC-009 **第一节 (五处差异)、第四节 (CI)、第五节 (手册)、第六节 (本机边界)**,
第八节验收里除"额度/配额/重置"之外的那几条, 第九节变异 4/5。

---

## 文件边界

**可写**:

```
docker-compose.prod.yml                  (新建, 覆盖层)
deploy/Caddyfile                         (新建)
apps/web/Dockerfile                      (锁文件 + npm ci)
apps/api/app/db.py                       (生产种子写 demo_marker 那一行)
apps/api/app/config.py
apps/api/alembic/versions/0010_*.py      (只为第 0 件加一列)
apps/api/app/services/agent_runtime.py   (只为第 0 件加结算调用)
apps/api/app/services/budget_service.py  (第 0 件)
.github/workflows/ci.yml + scripts/ci/   (docker job)
docs/deploy-runbook.md                   (新建)
apps/api/tests/                          (新测试)
```

**冻结**: `packages/` / `evals/` / `apps/web/src/` (只动 Dockerfile, 不动前端代码) /
`docs/` 里除 runbook 之外的一切。

---

## 七个易错点 (SPEC 里没写)

### 一、覆盖层撤端口, `ports: []` 很可能不生效

compose 合并覆盖层时 `ports` 是**追加**不是替换。要真正撤掉得用
`!override` / `!reset` 标签 (Compose v2.24+), 或者干脆把 db/api/web 的 `ports`
从基础文件挪进一个 `docker-compose.dev.yml`, 让基础文件本身不开口。
**两条路都行, 但必须实测**: `ss -ltn` (或 `docker compose ps --format`) 逐个数,
**断言宿主机上只有 80/443**。不要只看配置文件读起来对不对 ——
这一处"看起来撤了、其实没撤"和没写是一模一样的。

### 二、SSE 过了 Caddy 还要能一步步冒出来

`/agent-tasks/{id}/events` 是 SSE。**只断言它返回 200 是不够的** ——
带缓冲的反代会把整条流攒到结束再吐, 状态码一样漂亮, 而 Studio 的时间线
"一条条往下长"正是这个项目最拿得出手的那一屏。
验收要断言**第一个事件在 N 秒内到达**, 而不是"最后拿到了全部事件"。

### 三、`demo_marker` 那一行不许长到开发库里

生产种子写它, **开发库不能有** —— 否则重置脚本的护栏对你自己的开发库也放行,
而那正是第一段刚立起来的东西。用一个显式开关 (例如
`SENTINEL_APPLY_DEMO_MARKER`, 默认 false, 只有生产覆盖层里打开),
不要用 `environment != development` 这种间接判据。
**配一条测试**: 默认配置起库, `demo_marker` 必须是空的。

### 四、1 GB 内存那条验收, 先证明限制真的生效

`deploy.resources.limits.memory` 在**非 swarm 的 `docker compose up` 下不生效**,
要用 `mem_limit`。写错了的表现是"验收全过" —— 因为根本没限制。
**所以先做一次反向验证**: 故意跑一个吃内存的容器, 确认它真的被 OOM 干掉,
再拿这套限制去跑验收。这是本项目第九次记"判据会不会恒真", 这一次它藏在
一个 compose 字段名里。

### 五、`npm ci` 之前先确认锁文件是同步的

W5 已经去掉了 `npm ci || npm install` 那个 fallback, 所以锁文件与 `package.json`
不同步会直接红。改 Dockerfile 之前先在本机 `npm ci` 跑一遍。

### 六、docker job 红了要看得见原因

五条断言任何一条挂掉, **必须把容器日志打出来** (`docker compose logs --no-color`)。
CI 上没有终端可以进去看, 一个只说"断言失败"的 job 会让你在本机复现半小时。

### 七、runbook 是"写"不是"执行"

`docs/deploy-runbook.md` 这一段**只写不跑** —— 机器等投递前再开。
但它要写成可逐条打勾、半小时能走完的东西, 并且**"怎么关干净"与"怎么开"同等篇幅**
(SPEC 第五节)。里面不要出现任何真实密钥、账号 id、区域名的占位符以外的东西。

---

## 变异测试

SPEC 第九节 **4 / 5** 属于这一段:

- **4**: 删掉 `apps/api/Dockerfile` 里 seed CSV 那行 COPY → **docker job 必须红**。
  这一条是 W4 那笔债的正主, 也是唯一能证明新 job 真比旧 job 强的变异, 一定要做;
- **5**: 覆盖层里把撤端口那段删掉 → 端口断言必须红。

加上第 0 件的两条 (清扫回补 / 不重复回补), 逐条报变异了什么、哪条红了、红成什么形状。

---

## 老规矩

- **git 一律不执行**; `checkout` 不算只读; 只读命令加 `--no-optional-locks`。
  本人这次要等 SPEC-009 整份做完再统一提交, 所以工作树会一直脏, 属正常;
- 完成报告写进 `docs/ai-development/W6-第二段-完成报告.md`, **三节照旧**;
- 交之前跑 `make lint` + `apps/api` 全部测试 + **本机真起一次 prod 覆盖层**,
  结果写进报告;
- commit message 按 `CLAUDE.md` "commit message 的分寸" 那一节给建议, 不写内部角色。
