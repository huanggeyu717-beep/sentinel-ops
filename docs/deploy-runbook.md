# 部署开机手册 (SPEC-009 第五节)

> **状态: 只写不执行** —— 机器等投递前再开 (SPEC-009 分段实施第三段)。
> 本手册的每一条都设计成可打勾, 全程目标 **30 分钟内走完**。
> 生产形态 (compose 覆盖层 + Caddy + mem_limit 合计 736 MiB) 已在本机完整
> 验收过, 开机器跑的是**同一份文件、同一份镜像**; 本机验不到的五件 (真证书 /
> 公网延迟 / Budgets 告警送达 / 1 GB 实机表现 / 宿主机空载开销) 见 SPEC-009
> 第六节与本手册第一节。注意本机验收证明的是"四个容器在各自上限内跑得动",
> **不是**"这台 1 GB 的机器跑得动" —— 后者要等实机 `free -m` 对完数。
>
> 手册里出现的一切 `<尖括号>` 都是占位符。**不出现任何真实密钥、账号 id、
> 区域名** —— 真值只活在服务器的 `.env` (权限 600) 与 AWS 控制台里。

## 〇、开机前的固定事实

- 架构: Lightsail 单实例 (1 GB Micro) + docker compose + Caddy (ADR-004)。
- 全部生产差异都在 `docker-compose.prod.yml` 覆盖层与 `deploy/Caddyfile`,
  基础 `docker-compose.yml` 一个字不改。
- 花钱护栏在数据库里 (迁移 0010 的 CHECK), 不靠这台机器上的任何配置。
- 已知边界: 单点。实例挂了演示即不可用, README 写明"本地一键复现是权威路径"。

## 一、开机 (Lightsail + 系统准备)

- [ ] Lightsail 控制台建实例: **1 GB (Micro)**, OS 选 Ubuntu LTS,
      区域选定后记在下面"关机检查单"里 (关机时要回同一个区域找资源)。
- [ ] **不申请额外弹性 IP** (ADR-004 成本护栏第 2 条): 用实例自带的公网 IP;
      如果一定要静态 IP, 用 Lightsail 免费附着的那一个, 且关机检查单里登记。
- [ ] 防火墙 (Lightsail networking): 只开 **22 (仅本人 IP) / 80 / 443**。
      数据库端口不开 —— 覆盖层已把 5433 收回, 这里是第二道。
- [ ] SSH 上去: 装 docker + compose 插件, 当前用户加进 docker 组。
      `docker compose version` 必须 ≥ v2.24 (覆盖层用了 `!override` 标签)。
- [ ] **`free -m` 量一次空载开销** (装完 docker、还没起任何容器时)。
      覆盖层的 mem_limit 合计按"整机 1024 − 宿主机开销 ~288 = 736 MiB"定
      (db 320 / api 320 / caddy 64 / web 32), 其中 ~288 是**估值** ——
      本机验不到宿主机开销, 这是 SPEC-009 第六节之外的第五件本机边界。
      实测 used + 合理余量对不上 ~288 就按实测值重新分配四个 mem_limit,
      改完在实机上重跑一遍 `scripts/ci/test-docker.sh` 再继续。
- [ ] `git clone <本仓库>`; checkout 到投递用的那个 tag。

## 二、密钥与环境 (`.env`, 协作红线的服务器侧延伸)

- [ ] 在服务器上创建 `.env`, **用 `read -rs` 读入, 不上屏、不进 shell 历史**
      (zsh 下不要用 `read -p`):

      ```bash
      cd <仓库根>
      touch .env && chmod 600 .env
      read -rs JWT && echo "SENTINEL_JWT_SECRET=$JWT" >> .env && unset JWT
      read -rs ARK && echo "SENTINEL_LLM_API_KEY=$ARK" >> .env && unset ARK
      ```

      JWT 密钥现造: 本机 `openssl rand -hex 32`, 经剪贴板粘进 `read -rs`
      后**清剪贴板**。方舟 key 从控制台复制, 同样处理。
- [ ] `ls -l .env` 确认权限 `-rw-------` (600)。
- [ ] 数据库口令: 演示库不暴露端口, 保持 compose 默认即可; 若要更换,
      `.env` 里加 `POSTGRES_PASSWORD=<新口令>` (基础 compose 已读它)。
- [ ] **红线复述**: key 不进镜像、不进 compose 明文、不进聊天窗口/截图;
      一旦出现在任何聊天记录或剪贴板历史里, 按已泄露处置 —— 控制台删掉重建。

## 三、起服务与冒烟

- [ ] 起生产形态 (与本机验收、CI docker job 同一条命令):

      ```bash
      docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait
      ```

- [ ] 域名 DNS: `<演示域名>` A 记录指向实例公网 IP。
- [ ] 把站点地址从 localhost 切到真域名: `.env` 里加
      `SENTINEL_SITE_ADDRESS=<演示域名>`, 重启 caddy 容器。Caddy 对真域名
      自动做 ACME 挑战签 Let's Encrypt 真证书 —— 这是本机验不到的第一件,
      **开机后必须亲眼看到浏览器锁头图标**。
- [ ] 冒烟 (照 CI docker job 的断言逐条来, 服务器上直接跑同一份脚本也行:
      `bash scripts/ci/test-docker.sh` 需要把 BASE 换成真域名, 或手动):
  - [ ] `https://<演示域名>/api/health` 返回 200;
  - [ ] 首页打得开, 平面图上 5 个传感器在位;
  - [ ] 宿主机 `ss -ltn` 逐个数: 对公网只有 80/443 (22 只对本人 IP);
  - [ ] `http://<公网IP>:8000` / `:5433` / `:5173` 全部连不上;
  - [ ] 用 viewer 账号登录, 点开一条 Agent 任务, 时间线**一条条往下长**
        (SSE 没被攒流);
  - [ ] 建一条 Agent 任务真跑通 (这是唯一一次真实调用, 预算内)。
- [ ] 创建 admin (生产种子刻意不含它, SPEC-009 第一节第 5 条),
      随机口令当场生成、只显示一次:

      ```bash
      docker compose -f docker-compose.yml -f docker-compose.prod.yml \
        exec api python - <<'PY'
      import asyncio, secrets
      from app.db import _dsn
      from app.services.auth_service import hash_password
      import asyncpg

      async def main() -> None:
          pw = secrets.token_urlsafe(16)
          conn = await asyncpg.connect(_dsn())
          try:
              uid = await conn.fetchval(
                  "INSERT INTO users (email, password_hash, display_name) "
                  "VALUES ('admin@example.com', $1, 'Admin') "
                  "ON CONFLICT (email) DO UPDATE SET password_hash = $1 "
                  "RETURNING id", hash_password(pw))
              await conn.execute(
                  "INSERT INTO user_roles (user_id, role_id) "
                  "SELECT $1, id FROM roles WHERE name = 'admin' "
                  "ON CONFLICT DO NOTHING", uid)
          finally:
              await conn.close()
          print("admin 口令 (只显示这一次):", pw)

      asyncio.run(main())
      PY
      ```

      口令进本人密码管理器, 不落任何文件。

## 四、每日重置与成本护栏

- [ ] 每日 04:00 UTC 重置 (SPEC-009 第三节)。宿主机 crontab:

      ```
      0 4 * * * cd <仓库根> && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api python scripts/ops/reset_demo_data.py --base-url http://localhost:8000 && docker compose -f docker-compose.yml -f docker-compose.prod.yml restart api >> /var/log/sentinel-reset.log 2>&1
      ```

      注意两件: ① 脚本自己带护栏 —— 库里没有 demo_marker 那一行就拒绝执行,
      所以这条 cron 抄到任何别的机器上都是安全的; ② 重置后**必须重启 api**:
      引擎的内存状态 (已加载的发布策略) 是陈的 (W6 第一段报告第一节第 9 条)。
- [ ] **AWS Budgets 两级告警: $5 与 $10**, 通知发到本人邮箱 (ADR-004 成本
      护栏第 1 条)。告警是否真会送达本机验不到 —— 设完**手动降阈值触发一次**
      确认邮件收得到, 再调回去。
- [ ] CloudWatch 日志组 (若启用) 设保留期 7 天, 不留无限期日志 (同上第 3 条)。
- [ ] 模型侧花钱护栏无需配置 —— 它在数据库里: 日预算 ¥3、单账号 3 条/天、
      预扣超限的 UPDATE 物理上写不进去。方舟控制台侧再设一道余额告警。
- [ ] **日历提醒现在就设** (不是关机时才设): 每两周一条"演示机还开着,
      要不要关?" —— 求职结束后忘记关是这套东西唯一会长期扣钱的方式。

## 五、关机 (与开机同等篇幅 —— 忘关才是最贵的故障)

按顺序打勾, 每一条都有"验证到什么程度":

- [ ] README 先改: 摘掉演示链接, 换成"演示环境已下线, 本地
      `docker compose up` 一键复现是权威路径"。**先改文案再关机器**,
      别让公开的 README 指着一个死链接。
- [ ] 服务器上 `docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v`
      —— 数据卷一并删 (演示数据没有保留价值, 真实读数的事实源在仓库里)。
- [ ] 删 `.env` (`shred -u .env` 或至少 `rm`), 它是这台机器上唯一的真密钥载体。
- [ ] **方舟控制台把这台机器用的 API key 删掉重建** —— 机器交还给云厂商后,
      磁盘上曾有过的 key 一律按已泄露处置 (红线的处置口径)。
- [ ] Lightsail 控制台**删除实例** (不是 stop —— stop 了照样按小时扣静态
      IP/快照类资源; 要留纪念就先做一份本地导出)。
- [ ] 回到开机检查单登记的区域, 逐项确认: 无游离的静态 IP、无快照、无
      多余日志组 —— 每一样都是"停了机还在扣"的项目。
- [ ] DNS 上删掉 `<演示域名>` 的 A 记录。
- [ ] AWS Budgets 的告警**留着** (免费), 它是"以为关干净了其实没有"的最后
      一道网; 次月 1 号看一眼账单是 $0 才算关账。
- [ ] 删掉第四节设的日历提醒, 换成一条一次性的"下月初核对 AWS 账单 = $0"。

## 六、故障速查 (开着期间)

- 起不来且日志说 `SENTINEL_JWT_SECRET 仍是默认值`: `.env` 没被读到或值是
  默认 —— 这是 SPEC-004 埋的启动闸在正确地工作, 不是 bug。
- compose 直接报 `SENTINEL_JWT_SECRET (openssl rand -hex 32), 不接受默认值`:
  覆盖层的 `:?` 守卫, 同上, 先补 `.env`。
- 演示额度 429: 看 `X-Error-Code` —— `daily_budget_exhausted` 是全站日预算
  (¥3) 打满, UTC 零点自动翻篇; `user_quota_exhausted` 换一个演示账号。
- 内存吃紧 (1 GB): `docker stats` 看是谁; mem_limit 的分配写在
  `docker-compose.prod.yml` 注释里, 调整后 `up -d` 滚动生效。
- 重置脚本拒绝执行 (退出码 2): 它在保护一个不是演示库的库 —— 先确认连的是
  哪个库, 不要用手工 INSERT demo_marker 的方式"修"它。
