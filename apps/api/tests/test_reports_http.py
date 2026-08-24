"""HTTP 层: 报告的五个端点 (SPEC-008 第八节 + 第十节验收里走 HTTP 的部分)。

打桩模型, 不走 cassette (与 test_agent_http 同一条理由): 路由层要验的是
权限档位 / 状态码 / 去重口径 / 立刻返回, 与模型输出无关。全流程零真实调用。
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_agent_helpers import clean_agent_tables, db  # noqa: F401
from test_report_task_service import GOOD_BODY, insert_incident

from app.services import agent_runtime
from app.services.llm_client import LLMResponse, LLMToolCall, ScriptedLLMClient

T0 = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
ALEX = "alex@example.com"    # operator
CHRIS = "chris@example.com"  # manager (跨用户去重的第二个人)


def tool(_tool_name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(tool_call=LLMToolCall(tool=_tool_name, arguments=arguments),
                       input_tokens=10, output_tokens=5)


def make_incident(**cols) -> int:
    async def go(conn):
        incident_id = await insert_incident(
            conn,
            assigned_employee_id=1, assigned_at=T0 + timedelta(minutes=3),
            acknowledged_by_employee_id=2,
            acknowledged_at=T0 + timedelta(minutes=12),
            **cols,
        )
        for kind, at in (("opened", T0), ("resolved", T0 + timedelta(hours=1))):
            await conn.execute(
                "INSERT INTO incident_events (incident_id, kind, actor, at) "
                "VALUES ($1, $2, 'system', $3)", incident_id, kind, at,
            )
        return incident_id

    return db(go)


@pytest.fixture
def llm(client):
    """把报告路由的模型客户端换成打桩 (照 test_agent_http 的 llm 夹具)。"""
    from app.main import app
    from app.routers import reports as reports_router

    # 默认空脚本: 422/404 这类走不到模型的用例, 依赖解析仍要有个客户端可给
    current: dict[str, Any] = {"llm": ScriptedLLMClient(script=[])}

    def use(script: list[LLMResponse]) -> ScriptedLLMClient:
        current["llm"] = ScriptedLLMClient(script=list(script))
        return current["llm"]

    app.dependency_overrides[reports_router.get_llm_client] = lambda: current["llm"]
    yield use
    app.dependency_overrides.pop(reports_router.get_llm_client, None)


@pytest.fixture(autouse=True)
def no_leftover_background(client):
    yield
    deadline = time.monotonic() + 10
    while agent_runtime.running_task_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert agent_runtime.running_task_count() == 0, "后台任务没清干净"


def wait_status(client, headers, task_id: int, statuses: set[str],
                timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/agent-tasks/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        snapshot = r.json()
        if snapshot["task"]["status"] in statuses:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 没在 {timeout}s 内进入 {statuses}")


def _generate(client, headers, incident_id: int) -> dict[str, Any]:
    r = client.post(f"/incidents/{incident_id}/report", headers=headers)
    assert r.status_code == 202, r.text
    return r.json()


# ===== 生成 =====


def test_create_report__202_then_awaiting_review_and_rendered(
    client, auth_headers, llm
):
    """主线: 点一下 -> 202 立刻返回 -> 后台跑完停在等人过目 -> 两个 GET 都取
    得到报告, 渲染后的正文里是真实值不是占位符。"""
    incident_id = make_incident()
    hdr = auth_headers(ALEX)
    llm([tool("create_report_draft", body=GOOD_BODY)])

    created = _generate(client, hdr, incident_id)
    assert created["created"] is True
    assert created["stage"] == "collecting"
    wait_status(client, hdr, created["task_id"], {"awaiting_review"})

    r = client.get(f"/incidents/{incident_id}/report", headers=hdr)
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert report["status"] == "draft"
    assert report["task_id"] == created["task_id"]
    assert report["bare_fact_attempts"] == 0
    assert report["dangling_ref_attempts"] == 0
    rendered = report["rendered"]
    assert rendered is not None
    assert "{{" not in rendered["handling"]
    assert "Bo Wang" in rendered["handling"]  # {{ack_by}} 渲染成真实人名
    # 按 id 取同一份
    r2 = client.get(f"/reports/{report['id']}", headers=hdr)
    assert r2.status_code == 200
    assert r2.json()["report"] == report


def test_create_report__unresolved_422(client, auth_headers, llm):
    incident_id = make_incident(status="open", resolved_at=None, resolved_by=None)
    r = client.post(f"/incidents/{incident_id}/report", headers=auth_headers(ALEX))
    assert r.status_code == 422
    assert "resolved" in r.json()["detail"]


def test_create_report__unknown_incident_404(client, auth_headers, llm):
    r = client.post("/incidents/999999/report", headers=auth_headers(ALEX))
    assert r.status_code == 404


def test_create_report__dedupe_two_users_200_same_task(client, auth_headers, llm):
    """去重跨用户 (雷区 6): 第二个用户点同一条事故, 200 + 同一个 task_id,
    不新建任务、不报 4xx (验收 9)。"""
    incident_id = make_incident()
    llm([tool("create_report_draft", body=GOOD_BODY)])
    first = _generate(client, auth_headers(ALEX), incident_id)
    wait_status(client, auth_headers(ALEX), first["task_id"], {"awaiting_review"})

    r = client.post(f"/incidents/{incident_id}/report", headers=auth_headers(CHRIS))
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]


# ===== 权限档位 (SPEC-008 第八节的表, 逐字对齐) =====


def test_report_permissions__viewer_reads_but_cannot_write(
    client, auth_headers, viewer_headers, llm
):
    incident_id = make_incident()
    llm([tool("create_report_draft", body=GOOD_BODY)])
    # viewer 不能生成
    assert (
        client.post(f"/incidents/{incident_id}/report", headers=viewer_headers)
        .status_code == 403
    )
    created = _generate(client, auth_headers(ALEX), incident_id)
    wait_status(client, auth_headers(ALEX), created["task_id"], {"awaiting_review"})
    r = client.get(f"/incidents/{incident_id}/report", headers=viewer_headers)
    assert r.status_code == 200  # viewer 读得到
    report_id = r.json()["report"]["id"]
    # viewer 不能定稿也不能弃稿
    assert (
        client.post(f"/reports/{report_id}/finalize", headers=viewer_headers)
        .status_code == 403
    )
    assert (
        client.post(f"/reports/{report_id}/discard", headers=viewer_headers)
        .status_code == 403
    )
    # 未登录一律 401
    assert client.post(f"/incidents/{incident_id}/report").status_code == 401


# ===== 定稿 / 弃稿 =====


def _to_review(client, auth_headers, llm, incident_id: int) -> int:
    llm([tool("create_report_draft", body=GOOD_BODY)])
    created = _generate(client, auth_headers(ALEX), incident_id)
    wait_status(client, auth_headers(ALEX), created["task_id"], {"awaiting_review"})
    r = client.get(f"/incidents/{incident_id}/report", headers=auth_headers(ALEX))
    report_id: int = r.json()["report"]["id"]
    return report_id


def test_finalize__operator_can_then_repeat_conflicts(client, auth_headers, llm):
    """reports:finalize 给 operator+ (能起草的就能定稿, 不做行级归属);
    重复定稿 409。"""
    incident_id = make_incident()
    report_id = _to_review(client, auth_headers, llm, incident_id)
    r = client.post(f"/reports/{report_id}/finalize", headers=auth_headers(ALEX))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "final"
    assert (
        client.post(f"/reports/{report_id}/finalize", headers=auth_headers(ALEX))
        .status_code == 409
    )


def test_discard_final__frees_slot_for_regeneration(client, auth_headers, llm):
    """弃已定稿的 -> 部分唯一索引让位 -> 同一条事故能再生成 (SPEC-008 第八节:
    不许弃 final 等于定稿后永远开不出第二份)。"""
    incident_id = make_incident()
    report_id = _to_review(client, auth_headers, llm, incident_id)
    assert (
        client.post(f"/reports/{report_id}/finalize", headers=auth_headers(ALEX))
        .status_code == 200
    )
    r = client.post(f"/reports/{report_id}/discard", headers=auth_headers(ALEX))
    assert r.status_code == 200, r.text
    # 该事故当前无有效报告
    assert (
        client.get(f"/incidents/{incident_id}/report", headers=auth_headers(ALEX))
        .status_code == 404
    )
    # 能重开 (新任务, 不是去重命中)
    llm([tool("create_report_draft", body=GOOD_BODY)])
    again = _generate(client, auth_headers(ALEX), incident_id)
    assert again["created"] is True
    wait_status(client, auth_headers(ALEX), again["task_id"], {"awaiting_review"})


def test_get_report__unknown_404(client, auth_headers):
    assert (
        client.get("/reports/999999", headers=auth_headers(ALEX)).status_code == 404
    )
