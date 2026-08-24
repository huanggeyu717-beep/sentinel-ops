"""探针插件 (viewer 401): 只旁观, 不改任何断言、不动任何测试。

要回答的问题只有一个: **同一次失败的跑里, viewer 的 id 变过没有?**

四路观测 (与任务书第一步点名的四项一一对应):

1. 每次签发 token 时: sub 是多少、对应哪个邮箱、此刻库里 viewer 的 id;
2. 每条用例前后各查一次 viewer 的 id, 变了就记下 "变化发生在哪条用例";
3. get_user 返回 None 的那一刻: 查的是哪个 id、此刻库里 viewer 的 id;
4. 整个会话结束时: users 全表 + 序列状态。

由 probe_viewer401.py 通过 `pytest -p probe_viewer401_plugin` 装载, 不要单独用。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

LOG_PATH = os.environ.get("PROBE_VIEWER401_LOG", "scripts/dev/probe_viewer401.log")
DSN = os.environ.get(
    "SENTINEL_TEST_DATABASE_URL",
    "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel_test",
).replace("+asyncpg", "")

_state: dict[str, Any] = {"patched": False, "viewer": "<未查过>", "current": "<无>"}


def _log(msg: str) -> None:
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def _fetch(sql: str, *args: Any) -> list[Any]:
    import asyncpg

    async def go() -> list[Any]:
        conn = await asyncpg.connect(DSN)
        try:
            return list(await conn.fetch(sql, *args))
        finally:
            await conn.close()

    def work() -> list[Any]:
        return asyncio.run(go())

    # create_token 在应用的事件循环里被调 (登录路由), 那里不能 asyncio.run ——
    # 丢进独立线程跑, 否则探针自己把登录打炸, 污染观测对象
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return work()
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(work).result()


def _viewer_id() -> Any:
    try:
        rows = _fetch("SELECT id FROM users WHERE email = 'viewer@example.com'")
    except Exception as e:  # 建库前/降级中途 users 表可能不存在, 照记不炸
        return f"<查询失败: {type(e).__name__}>"
    return rows[0]["id"] if rows else "<无此行>"


def _patch_auth() -> None:
    """包一层 create_token / get_user。conftest 已 import 过 app, 这里才 import 安全。"""
    if _state["patched"]:
        return
    _state["patched"] = True

    from sqlalchemy import text as sa_text

    from app.services import auth_service

    orig_create = auth_service.create_token

    def create_token(user_id: int, now: Any = None) -> Any:
        rows = _fetch("SELECT email FROM users WHERE id = $1", user_id)
        email = rows[0]["email"] if rows else "<库里已无此 id>"
        _log(
            f"[签发 token] sub={user_id} email={email} "
            f"此刻 viewer id={_viewer_id()} (用例 {_state['current']})"
        )
        return orig_create(user_id, now)

    auth_service.create_token = create_token  # type: ignore[assignment]

    orig_get_user = auth_service.get_user

    async def get_user(session: Any, user_id: int) -> Any:
        user = await orig_get_user(session, user_id)
        if user is None:
            row = (
                await session.execute(
                    sa_text("SELECT id FROM users WHERE email = 'viewer@example.com'")
                )
            ).scalar_one_or_none()
            _log(
                f"[get_user 返回 None] 查的 sub={user_id}, 此刻库里 viewer id={row} "
                f"(用例 {_state['current']})"
            )
        return user

    auth_service.get_user = get_user  # type: ignore[assignment]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _patch_auth()
    _state["current"] = item.nodeid
    vid = _viewer_id()
    if vid != _state["viewer"]:
        _log(f"[viewer id 变化] {_state['viewer']} -> {vid} (发现于 {item.nodeid} 之前)")
        _state["viewer"] = vid


def pytest_runtest_teardown(item: pytest.Item) -> None:
    vid = _viewer_id()
    if vid != _state["viewer"]:
        _log(f"[viewer id 变化] {_state['viewer']} -> {vid} (发生在 {item.nodeid} 执行期间)")
        _state["viewer"] = vid


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    try:
        rows = _fetch("SELECT id, email FROM users ORDER BY id")
        seq = _fetch("SELECT last_value, is_called FROM users_id_seq")
        _log("[收尾] users 全表: " + ", ".join(f"{r['id']}={r['email']}" for r in rows))
        _log(
            f"[收尾] users_id_seq last_value={seq[0]['last_value']} "
            f"is_called={seq[0]['is_called']} pytest 退出码={exitstatus}"
        )
    except Exception as e:
        _log(f"[收尾] 查询失败: {e!r}")
