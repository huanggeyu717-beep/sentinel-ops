"""SPEC-005 前置 B 验收: 演练触发接口。

覆盖: viewer 403 / 并发同场景 409 且跑完可重启 / 进度递增到 completed /
失败态带 error 不静默消失 / 未知场景与未知 drill_id 404 / 内存记录有上界。

真实场景文件跑一遍要几十秒, 多数用例往 tmp_path 写迷你场景并 monkeypatch
SCENARIOS_DIR, 不碰仓库的 scenarios/; 列表用例反过来用真实目录, 顺带验证
"场景挪到仓库根之后 API 找得到" 这件事本身。
"""
from __future__ import annotations

import time

import pytest

from app.config import settings
from app.services import drill_service


@pytest.fixture(scope="module")
def op_hdr(auth_headers):
    return auth_headers("alex@example.com")  # operator


@pytest.fixture(autouse=True)
def clean_drills():
    drill_service.reset()
    yield
    drill_service.reset()


def write_scenario(dirpath, stem, events):
    lines = [f"name: {stem}", "events:"]
    for ev in events:
        pairs = ", ".join(f"{k}: {v}" for k, v in ev.items())
        lines.append(f"  - {{{pairs}}}")
    (dirpath / f"{stem}.yaml").write_text("\n".join(lines), encoding="utf-8")


def heartbeats(*at_s):
    return [{"at_s": t, "kind": "heartbeat", "device_id": "DrillDev", "uptime_ms": 1}
            for t in at_s]


def get_drill(client, hdr, drill_id):
    r = client.get(f"/drills/{drill_id}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def wait_terminal(client, hdr, drill_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = get_drill(client, hdr, drill_id)
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"演练 {drill_id} 在 {timeout}s 内没进入终态")


# ===== 场景列表 =====

def test_list_scenarios__returns_name_count_duration_from_repo_root(client, op_hdr):
    rows = {s["scenario"]: s for s in
            client.get("/drills/scenarios", headers=op_hdr).json()["scenarios"]}
    assert rows["basic_spill"]["events_total"] == 4
    assert rows["basic_spill"]["duration_s"] == 75
    assert rows["basic_spill"]["name"] == "basic_spill"
    assert rows["multi_sensor_escalation"]["events_total"] == 8
    assert rows["multi_sensor_escalation"]["duration_s"] == 290


def test_drills_without_login__401(client):
    assert client.get("/drills/scenarios").status_code == 401
    assert client.post("/drills/basic_spill").status_code == 401


# ===== 权限: 决策 6, operator 及以上才能触发 =====

def test_start_drill__viewer_403(client, viewer_headers):
    r = client.post("/drills/basic_spill", headers=viewer_headers)
    assert r.status_code == 403


# ===== 404 边界 =====

def test_start_drill__unknown_scenario_404(client, op_hdr):
    assert client.post("/drills/no_such_scenario", headers=op_hdr).status_code == 404


def test_drill_status__unknown_id_404(client, op_hdr):
    assert client.get("/drills/ffffffffffff", headers=op_hdr).status_code == 404


# ===== 生命周期 =====

def test_start_drill__conflict_409_while_running_then_ok_after_finish(
    client, op_hdr, tmp_path, monkeypatch
):
    monkeypatch.setattr(drill_service, "SCENARIOS_DIR", tmp_path)
    write_scenario(tmp_path, "conflict_demo", heartbeats(0, 15))  # x10 加速下跑 1.5s

    first = client.post("/drills/conflict_demo", headers=op_hdr)
    assert first.status_code == 202
    assert client.post("/drills/conflict_demo", headers=op_hdr).status_code == 409

    wait_terminal(client, op_hdr, first.json()["drill_id"])
    rerun = client.post("/drills/conflict_demo", headers=op_hdr)  # 跑完之后可以再来
    assert rerun.status_code == 202
    wait_terminal(client, op_hdr, rerun.json()["drill_id"])


def test_drill_progress__events_sent_increases_until_completed(
    client, op_hdr, tmp_path, monkeypatch
):
    monkeypatch.setattr(drill_service, "SCENARIOS_DIR", tmp_path)
    write_scenario(tmp_path, "progress_demo", heartbeats(0, 4, 8, 12, 16, 20))  # 跑 2s

    started = client.post("/drills/progress_demo", headers=op_hdr)
    assert started.status_code == 202
    body = started.json()
    drill_id = body["drill_id"]
    assert body["events_total"] == 6
    assert body["speed"] == settings().drill_speed  # 倍率回显 (SPEC: SENTINEL_DRILL_SPEED)

    observed = [body["events_sent"]]
    final = None
    deadline = time.time() + 15
    while time.time() < deadline:
        snap = get_drill(client, op_hdr, drill_id)
        observed.append(snap["events_sent"])
        if snap["status"] in ("completed", "failed"):
            final = snap
            break
        time.sleep(0.1)
    assert final is not None and final["status"] == "completed", final
    assert final["events_sent"] == 6
    assert observed == sorted(observed), f"进度必须单调递增: {observed}"
    assert min(observed) < 6, "轮询理应观察到中间进度, 而不是一步到位"
    # 事件确实走了与 /ingest 同一条 service 路径 -> 心跳落到了 device_heartbeats
    devices = client.get("/status/devices", headers=op_hdr).json()["devices"]
    assert "DrillDev" in {d["device_id"] for d in devices}


def test_drill_failure__invalid_event_marks_failed_with_error(
    client, op_hdr, tmp_path, monkeypatch
):
    monkeypatch.setattr(drill_service, "SCENARIOS_DIR", tmp_path)
    # sensor_state 缺 sensor_id: pydantic 校验(与 /ingest 同一模型)会当场拒绝
    write_scenario(tmp_path, "broken_demo",
                   [{"at_s": 0, "kind": "sensor_state", "device_id": "BadDev"}])

    drill_id = client.post("/drills/broken_demo", headers=op_hdr).json()["drill_id"]
    body = wait_terminal(client, op_hdr, drill_id)
    assert body["status"] == "failed"
    assert body["error"] and "sensor_id" in body["error"]
    assert body["events_sent"] == 0
    # 失败的演练仍可查询, 没有静默消失
    assert client.get(f"/drills/{drill_id}", headers=op_hdr).status_code == 200


def test_drill_history__bounded_drops_oldest(client, op_hdr, tmp_path, monkeypatch):
    monkeypatch.setattr(drill_service, "SCENARIOS_DIR", tmp_path)
    monkeypatch.setattr(settings(), "drill_history_limit", 3)
    write_scenario(tmp_path, "tiny", heartbeats(0))

    ids = []
    for _ in range(4):
        r = client.post("/drills/tiny", headers=op_hdr)
        assert r.status_code == 202
        ids.append(r.json()["drill_id"])
        wait_terminal(client, op_hdr, ids[-1])  # 跑完再起下一个, 避免 409

    assert client.get(f"/drills/{ids[0]}", headers=op_hdr).status_code == 404  # 最旧的被丢弃
    for kept in ids[1:]:
        assert client.get(f"/drills/{kept}", headers=op_hdr).status_code == 200


def test_start_drill__stays_synchronous_so_the_conflict_check_is_atomic():
    """409 成立的前提: start_drill 是同步函数。

    "先查有没有同名演练在跑, 再登记" 这两步之间**一旦出现 await**, 事件循环就会
    切去处理另一个请求, 两个请求都会查到"没有在跑"然后双双登记 —— 锁就漏了。
    同步函数在单线程事件循环里跑完才交出控制权, 所以这两步是原子的。

    实测: 同步版本 5 个并发启动 = 1 成功 + 4 个 409;
    改成 async 并在中间 await 一次, 5 个全部成功。

    现有的 409 用例走 TestClient, 请求是串行的, **抓不到这种交错**。
    所以这里直接把"必须是同步函数"这个前提钉住。
    """
    import inspect

    from app.services import drill_service

    assert not inspect.iscoroutinefunction(drill_service.start_drill), (
        "start_drill 变成协程函数了: 冲突检查与登记之间若插入 await, 同场景并发就不再被 409 拦住。"
        "需要 await 的活请放进 _run 的后台任务里, 或改用显式的 asyncio.Lock。"
    )
