#!/usr/bin/env bash
# CI 的 docker job: 起**生产形态**的容器跑断言 (SPEC-009 第四节)。
#
# 旧 job 只 `docker compose build api device-sim`, 拦得住"快炸" (缺 COPY 源,
# build 当场失败), 拦不住"慢炸" (根本没写的 COPY 行 —— W4 的 seed CSV 就是
# 这么漏的: build 全绿、容器起得来、界面打得开, 只有有人点"模拟"才断)。
# 这里起的是 docker-compose.prod.yml 覆盖层: 同一次 CI 顺带验证端口收回、
# Caddy 反代、SSE 不被攒流、1 GB mem_limit —— 将来上生产的就是这套文件。
#
# 本机跑法 (与 CI 同一份脚本): bash scripts/ci/test-docker.sh
# 需要 docker; SENTINEL_JWT_SECRET 不设时用一个一次性值 (只进这套即抛容器)。
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export SENTINEL_JWT_SECRET="${SENTINEL_JWT_SECRET:-ci-docker-job-throwaway-not-a-real-secret}"
# 独立的 compose project: 收尾的 `down -v` 只删**这套**的卷。不带 -p 的话
# 本机跑这脚本会把开发用的 pgdata 卷 (sentinel/sentinel_eval 两个库) 连坐删掉
COMPOSE=(docker compose -p sentinel-prod-ci -f docker-compose.yml -f docker-compose.prod.yml)
BASE=https://localhost
# -k: 本机/CI 走 Caddy 内部 CA, 信任链验不过是预期 —— 真证书要真域名
# (SPEC-009 第六节第 1 条写明的边界)
CURL=(curl -sk)
WORKDIR=$(mktemp -d)

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # 易错点六: CI 上没有终端可以进去看, 一个只说"断言失败"的 job 会让人
    # 在本机复现半小时 —— 红了必须把容器日志打出来
    section "断言失败 (exit=$status), 容器日志如下"
    "${COMPOSE[@]}" logs --no-color --tail 300 || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
  exit "$status"
}
trap cleanup EXIT

fail() { echo "断言失败: $*" >&2; exit 1; }

section "断言 5/5: web 与 sim-replay 也在 build 目标里"
"${COMPOSE[@]}" --profile replay build api device-sim web sim-replay

section "起生产形态 (端口收回 + Caddy + mem_limit 合计 736 MiB, 给宿主机留余量)"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --wait --wait-timeout 240 db api web caddy

section "断言 1/5: /health 200 (迁移真跑过、app.main 真 import 得进去)"
"${COMPOSE[@]}" exec -T api python -c \
  "import urllib.request; assert urllib.request.urlopen('http://localhost:8000/health').status == 200"

section "断言 3/5: 容器里 seed CSV 在位 (W4 那笔债的钉子, 变异 4 的正主)"
"${COMPOSE[@]}" exec -T api test -f apps/device-sim/seed/waterlevel_readings.csv \
  || fail "apps/api/Dockerfile 少了 seed CSV 的 COPY: 容器里每条 Agent 任务都会在 simulating 落 dead_letter"

section "断言 4/5: Caddy 反代真的通 (/api/health 与首页各 200)"
# caddy 刚起, 给它最多 30 秒就绪; --retry-connrefused 只重试连接类失败
"${CURL[@]}" --retry 10 --retry-delay 3 --retry-connrefused -o /dev/null "$BASE/api/health" \
  || fail "经 Caddy 访问 /api/health 不通"
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/api/health")
[ "$code" = "200" ] || fail "/api/health 经 Caddy 返回 $code"
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/")
[ "$code" = "200" ] || fail "首页经 Caddy 返回 $code"

section "断言 2/5: /status/sensors 5 个传感器 (种子真写进去了)"
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -c "$WORKDIR/cookies.txt" \
  -H 'Content-Type: application/json' \
  -d '{"email":"viewer@example.com","password":"sentinel-demo"}' \
  "$BASE/api/auth/login")
[ "$code" = "200" ] || fail "演示账号 viewer 登录失败 ($code) —— 生产种子没种账号?"
"${CURL[@]}" -b "$WORKDIR/cookies.txt" "$BASE/api/status/sensors" | python3 -c '
import json, sys
ids = {s["sensor_id"] for s in json.load(sys.stdin)["sensors"]}
assert {1, 2, 3, 4, 5} <= ids, f"5 个在位传感器不全: {sorted(ids)}"
' || fail "/status/sensors 里种子传感器不全"

section "端口断言 (验收第一条, 变异 5 的正主): 宿主机上只有 80/443"
published=$(
  for c in $("${COMPOSE[@]}" ps -q); do
    docker inspect --format \
      '{{range $p, $b := .NetworkSettings.Ports}}{{range $b}}{{.HostPort}}{{"\n"}}{{end}}{{end}}' "$c"
  done | sort -un | xargs
)
echo "compose 发布到宿主机的端口: ${published:-<无>}"
[ "$published" = "80 443" ] \
  || fail "端口没收干净 (期望恰好 '80 443', 实得 '${published}') —— ports 的 !override 覆写被删了?"

section "SSE 断言 (易错点二): 第一个事件 5 秒内到达, 且流仍然开着"
# 裸插一条 clarifying 任务 (不终态, SSE 流不会自己关; 不发任何模型调用):
# 只断言 200 是不够的 —— 带缓冲的反代把整条流攒到结束再吐, 状态码一样漂亮
# head -n1: psql 对 DML 的 RETURNING 会在结果行后再打一行命令 tag ("INSERT 0 1")
task_id=$("${COMPOSE[@]}" exec -T db psql -U sentinel -d sentinel -tA -c \
  "INSERT INTO agent_tasks (user_id, task_type, input, status, stage, input_hash)
   VALUES (3, 'policy_compile', '{\"text\": \"sse probe\"}', 'clarifying', 'parsing', 'ci-sse-probe')
   RETURNING id" | head -n1)
[ -n "$task_id" ] || fail "SSE 探针任务没插进去"
set +e
"${CURL[@]}" -N --max-time 5 -b "$WORKDIR/cookies.txt" \
  -o "$WORKDIR/sse.out" "$BASE/api/agent-tasks/${task_id}/events"
rc=$?
set -e
[ "$rc" = "28" ] || fail "SSE 流在 5 秒内就结束了 (curl rc=$rc, 期望 28=仍开着被超时掐断)"
grep -q '^event:' "$WORKDIR/sse.out" \
  || fail "流开着但 5 秒内一个事件都没冒出来 —— 反代在攒流 (Studio 时间线会一次性倒完)"
echo "首个事件已在流关闭前到达: $(grep -m1 '^event:' "$WORKDIR/sse.out")"

section "docker job 全部断言通过"
