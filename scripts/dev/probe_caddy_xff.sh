#!/usr/bin/env bash
# 探针 (W6 第二段复核): Caddy 的 reverse_proxy 对**外来的** X-Forwarded-For
# 是覆盖还是追加? 这决定 SENTINEL_TRUST_PROXY_HEADERS=true 之后, 登录限流
# 取的那个 IP (rate_limit.py 取 split(",")[0], 即第一个) 是不是客户端可控。
#
# 自足: 一个 caddy 容器里两个站点 —— 8080 反代到 8081, 8081 把收到的
# X-Forwarded-For 原样回显。不依赖本项目的任何服务。
set -euo pipefail
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"; docker rm -f probe-caddy-xff >/dev/null 2>&1 || true' EXIT
cat > "$tmp/Caddyfile" <<'CADDY'
{
	auto_https off
}
:8080 {
	reverse_proxy localhost:8081
}
:8081 {
	respond "upstream-saw: [{header.X-Forwarded-For}]" 200
}
CADDY
docker run -d --rm --name probe-caddy-xff -p 18080:8080 \
	-v "$tmp/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine >/dev/null
for _ in $(seq 30); do curl -sf localhost:18080 >/dev/null 2>&1 && break; sleep 0.3; done
echo "--- 不带 XFF (基线) ---"
curl -s localhost:18080
echo; echo "--- 带伪造 XFF: 9.9.9.9 ---"
curl -s -H 'X-Forwarded-For: 9.9.9.9' localhost:18080
echo; echo "--- 带伪造 XFF: 1.1.1.1, 2.2.2.2 ---"
curl -s -H 'X-Forwarded-For: 1.1.1.1, 2.2.2.2' localhost:18080
echo
