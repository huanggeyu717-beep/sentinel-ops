"""每臂一个独立 API 子进程 (uvicorn)。

迁移与 dev seed 由它的 lifespan 自己跑 —— 与 CI/开发/Docker 同一条建表路径
(SPEC-007 第七节第 4 条 "复用一条验过四周的路")。工作目录固定仓库根: Settings
的 env_file=".env" 从 CWD 解析, API key 只活在 .env 里, 不经过本进程的参数。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 90


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ApiProcess:
    def __init__(self, env_overrides: dict[str, str], port: int | None = None) -> None:
        self.port = port if port is not None else free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._overrides = env_overrides
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            **self._overrides,
            # 本地包不装 venv (pytest.ini 同一份路径), 子进程要自带
            "PYTHONPATH": os.pathsep.join(
                str(p) for p in (
                    REPO / "apps" / "api",
                    REPO / "packages" / "policy_engine",
                    REPO / "packages" / "scenario",
                )
            ),
        }
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=REPO, env=env,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"API 子进程启动即退出 (exit={self._proc.returncode})"
                )
            try:
                if httpx.get(f"{self.base_url}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"API 子进程 {_HEALTH_TIMEOUT_S} 秒内没就绪")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=15)
        self._proc = None
