#!/usr/bin/env bash
# 本机 lint 前的护栏: 确认本机工具链与 CI 一致。
# 版本不一致时, "本机绿" 不代表 "CI 绿" —— W1 收尾时就栽在这里 (见 ADR-005)。
#
# 注意: 本文件的提示信息一律用 printf '...%s...' "$var" 输出, 不用 heredoc 内嵌变量。
# 原因: macOS 自带 bash 3.2 在解析 "$var。" 这种"变量后面紧跟中文标点"时,
# 会把中文字节吞进变量名, 配合 set -u 直接报 unbound variable。
# printf 把文案和变量彻底分开, 不给 shell 解析文案的机会。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# --- ruff: 必须完全一致, 不一致直接拦下 ---------------------------------------
want_ruff="$(grep -E '^ruff==' requirements-dev.txt | cut -d= -f3 || true)"
have_ruff="$(ruff --version 2>/dev/null | awk '{print $2}' || true)"

if [ -z "${want_ruff:-}" ]; then
  echo "requirements-dev.txt 里没找到 ruff== 的锁定版本, 请检查该文件。" >&2
  exit 1
fi

if [ -z "${have_ruff:-}" ]; then
  printf '找不到 ruff。请先执行:\n  make dev-tools\n' >&2
  exit 1
fi

if [ "${have_ruff}" != "${want_ruff}" ]; then
  printf '本机 ruff 版本与 CI 锁定的不一致:\n' >&2
  printf '  本机: %s\n' "${have_ruff}" >&2
  printf '  CI  : %s\n' "${want_ruff}" >&2
  printf '本机检查结果不代表 CI 结果。请执行:\n  make dev-tools\n' >&2
  exit 1
fi

# --- Python: 只提醒, 不拦 ------------------------------------------------------
# 期望版本直接从 CI 配置里读, 两边不可能各写各的。
want_py="$(sed -n 's/.*python-version: *"\([0-9][0-9.]*\)".*/\1/p' \
           .github/workflows/ci.yml | head -1 || true)"
have_py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"

if [ -n "${want_py:-}" ] && [ -n "${have_py:-}" ] && [ "${want_py}" != "${have_py}" ]; then
  printf '提醒: 本机 Python %s, CI 跑的是 %s\n' "${have_py}" "${want_py}" >&2
  printf '测试在本机过了不等于在 CI 过 (反之亦然)。不拦你, 但 CI 报错时先想到这一条。\n' >&2
fi

printf 'ruff %s (与 CI 一致) / python %s\n' "${have_ruff}" "${have_py:-未知}"
