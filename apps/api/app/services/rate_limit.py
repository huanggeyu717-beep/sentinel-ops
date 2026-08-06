"""登录限流 —— 按来源 IP 的滑动窗口 (SPEC-004 决策 10)。

**为什么按 IP 而不按账号**: 按账号锁 (错 N 次锁定该邮箱) 本身就是个漏洞 ——
攻击者故意去错几次 chris@example.com, 就把真正的 Chris 关在门外了,
限流反过来变成拒绝服务的工具。按来源 IP 限则锁不到别人。

**只计失败**: 登录成功不计数, 且成功后清掉该 IP 的失败记录。
正常用户敲错两次再登对, 不该被自己之前的手误拖累; 而攻击者的尝试全是失败, 照样被计。

**已知边界** (都是刻意的, 不是遗漏):
- 计数在进程内存里。本项目 W6 是单实例部署, 够用; 多实例要换 Redis 一类共享存储。
  重启即清零 —— 对爆破的影响有限, bcrypt cost 12 本身已经把速度压到每次约 270ms。
- 同一 NAT 出口后面的多个用户共享一个 IP, 会互相影响。阈值取得比正常人手误宽得多,
  正常使用碰不到。
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import Request

from ..config import Settings

# 单进程内存计数上限: 防止攻击者轮换海量 IP 把内存撑爆。
# 超过就丢掉最旧的一批 —— 宁可放过少数, 不接受被拖垮。
_MAX_TRACKED_IPS = 10_000


class LoginRateLimiter:
    """按 IP 的滑动窗口计数器。时钟可注入, 方便测试不必真的等窗口过期。"""

    def __init__(self, attempts: int, window_seconds: int) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}

    def _now(self) -> float:
        # 用单调时钟: 系统时间被改动 (NTP 校时、手动改) 不会让窗口错乱
        return time.monotonic()

    def _prune(self, ip: str, now: float) -> deque[float]:
        window = self._failures.setdefault(ip, deque())
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            self._failures.pop(ip, None)
            return deque()
        return window

    def retry_after(self, ip: str) -> int | None:
        """还能不能再试。可以则返回 None; 不能则返回还要等多少秒。"""
        now = self._now()
        window = self._prune(ip, now)
        if len(window) < self.attempts:
            return None
        # 最早那次失败滑出窗口时才重新放行
        return max(1, int(window[0] + self.window_seconds - now) + 1)

    def record_failure(self, ip: str) -> None:
        now = self._now()
        if len(self._failures) >= _MAX_TRACKED_IPS and ip not in self._failures:
            self._failures.pop(next(iter(self._failures)), None)
        self._prune(ip, now)
        self._failures.setdefault(ip, deque()).append(now)

    def record_success(self, ip: str) -> None:
        """登录成功即清账: 手误不该拖累后续。"""
        self._failures.pop(ip, None)

    def reset(self) -> None:
        """仅供测试与本地调试。"""
        self._failures.clear()


def client_ip(request: Request, s: Settings) -> str:
    """取来源 IP。

    **默认不信 X-Forwarded-For**: 那是个客户端可以随便写的请求头, 直接采信等于
    把限流关掉 —— 攻击者每次换一个假的转发头就永远不会触顶。
    只有部署在自己的反向代理后面 (W6) 才把 SENTINEL_TRUST_PROXY_HEADERS 打开,
    且必须由代理**覆盖**而不是追加这个头。
    """
    if s.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
