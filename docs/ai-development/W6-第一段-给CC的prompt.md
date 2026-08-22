# W6 第一段 · 给 CC 的 prompt

> 依据 `docs/specs/SPEC-009-deployment.md` —— **第二节 (花钱护栏)、第三节 (重置)、
> 第七节 (迁移 0010)**, 第八节验收里属于这一段的那几条, 第九节变异 1/2/3/6。
>
> **纯本机, 零真实模型调用, 零花费。** 不碰 compose、不碰 Caddy、不碰 CI ——
> 那三样是第二段。

## 这一段的范围 (细节在 SPEC 里, 这里只划线)

迁移 `0010` 三张表 → 预扣/回补 service → 重置脚本与它的护栏 → 测试与变异测试。

---

## 文件边界

**可写**:

```
apps/api/alembic/versions/0010_*.py
apps/api/app/services/budget_service.py        (新建)
apps/api/app/services/agent_service.py         (只动建任务那条路径)
apps/api/app/routers/agent_tasks.py            (只动 429 那条路径)
apps/api/app/config.py                         (三个配置项)
scripts/ops/                                   (新建, 重置脚本)
scripts/ci/lib.sh                              (只加 LINT_TARGETS 那一行, 见易错点六)
apps/api/tests/                                (新测试)
```

**冻结**: `packages/` / `evals/` / `apps/web/` / `docs/` (报告除外) /
`docker-compose.yml` / `.github/`。

---

## 七个易错点 (SPEC 里没写, 而且写错了不会有任何东西报错)

### 一、预扣插在哪一步 —— 这是最容易写反的一处

`routers/agent_tasks.py` 的 `create_task` 现在的顺序是: **同步预留槽位 → 开事务 →
`agent_service.create_task` → 提交 → `spawn_task`**。

钱的预扣要插在**那个事务里面**, 并且在 `create_task` 返回之后、**只在 `created`
为真时**扣。三条理由都要成立:

- **去重命中 (`created=False`) 不能扣钱** —— 用户什么都没多得到, 扣了就是白扣;
- 预扣失败时**抛异常让整个事务回滚**, 任务行连同预扣一起消失。路由层现有的
  `except BaseException: release_task_slot()` 正好接得住, 不要另写一套;
- **不要写成"先查余额, 够就扣"**。那等于把护栏退回应用层, 并发下十个人同时读到
  "还有余额", 十条任务全起来 —— 这正是 SPEC 第二节要用一次事务内加法解决的事。

### 二、两个 429 必须能分辨

槽位满已经是 429 (`CapacityExceeded`), 额度用完也是 429。**用户看到同一个码,
要能分清是"系统忙, 等会儿再来"还是"今天的额度用完了, 明天再来"** ——
这两句话对应的下一步动作完全不同。错误码与文案分开, 各配一条测试。

### 三、`day` 用哪个时区, 要写死并就地注释

`llm_spend_daily.day` 的"今天"必须显式定成 **UTC 日期**, 不要图省事用数据库的
`current_date` (它跟服务器时区走)。开发机在 Europe/London、服务器在 UTC,
不写死的话"今天"的边界会随部署地漂 —— 而**这条漂移不会有任何东西报错**,
只会让某一天的额度莫名多出或少掉一个小时。

### 四、回补是另一个事务, 且回补失败不许让任务失败

钱多扣了是小事, 任务因为记账挂掉是大事。另外两条:

- 回补要用 `GREATEST(0, ...)` 兜住, **不许把 `spent` 减成负数**;
- **回补的数据来源是 `ai_usage` 的合计, 不是内存计数器** —— 与 SPEC-002
  "调用数要数 `ai_usage` 的行"同一条理由: 跨轮、跨进程、跨重启都成立。

### 五、测试放 `apps/api/tests/`, 不许进离线档

这一段测的就是数据库约束, 全部要真 Postgres。`evals/tests` 与 `packages/` 那一套
挂在 **engine job** 上, 它的依赖贫瘠**是一项被断言守着的资产**
(`test_offline_tests__only_import_what_the_unit_job_installs`)。
往那里塞一条要 asyncpg 的测试, 等于把 W5 刚修好的东西再拆一次。

### 六、重置脚本放 `scripts/ops/`, 而且新目录**必须进 `LINT_TARGETS`**

`scripts/ci/lib.sh` 第 42 行的
`LINT_TARGETS=(packages apps/api apps/device-sim evals scripts/dev)`
是检查目标的**单一事实源** (W5 刚收敛成这个形状)。新开一个 `scripts/ops/` 不加进去,
它就是又一个"一直合规、只是没人验证过"的洞 —— W5 把 `scripts/dev` 加进目标时
**零改动通过**, 那不是运气: 在被检查之前, "合规"和"不合规"长得一模一样。

(它不是临时脚本, 所以不放 `scripts/dev/`; 协作红线要求的是"落在仓库里", 满足。)

### 七、迁移的两件事

- **当前 head 是 `0009_evals_groundwork`**, 新迁移的 `down_revision` 指它;
- 用 `make migrate-new id=0010_deploy_guardrails m="..."`, 手写不用 autogenerate
  (本项目无 ORM 模型), **必须写 downgrade 并实测"降一步再升回"**。

---

## 变异测试

SPEC 第九节的 **1 / 2 / 3 / 6** 属于这一段 (4 和 5 是第二段的 CI 与 compose)。
逐条报: 变异了什么、哪条测试红了、**红成什么形状**。

**第 6 条要特别做到**: 额度耗尽那条断言要**把 limit 直接设成 0** 来测,
不要等它自然花完 —— "从没超过"与"检查失效"在外面看长得一模一样。
这是本项目第八次记这件事, 别让它变成第九次。

---

## 不要做的

- 不碰 `docker-compose.yml` / Caddy / `.github/` —— 第二段;
- 不碰 `docs/` (归评审方)。**SPEC 有问题写进报告那一节, 不要自己改 SPEC**;
- **不要顺手改 README**;
- 生产种子 (往库里写 `demo_marker` 那一步) 属于第二段, 这一段**只建表**;
  但重置脚本要能在"本机手工插一行 marker"之后跑通, **测试自己造数据**。

---

## 老规矩

- **git 一律不执行**; `checkout` 不算只读; 只读命令加 `--no-optional-locks`。
- 完成报告写进 `docs/ai-development/W6-第一段-完成报告.md`, **三节照旧**:
  与 SPEC 不一致的地方 (含你认为评审方写错的) / 自行新增的分支与写操作各由哪条
  测试守着 / 报数字必须带产生它的配置。**不要只打在终端里。**
- 交之前跑 `make lint` 与 `apps/api` 全部测试, 结果写进报告。
- 有一条新规矩: **commit message 不写内部角色与返工过程** (`CLAUDE.md` 新增的
  "commit message 的分寸")。你只负责给建议的 message, 照那一节的分寸写。
