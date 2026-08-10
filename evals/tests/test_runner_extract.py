"""extract 的纯函数半边: 数据库原料 -> CaseOutcome / 计量行。手造 record, 不连库。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evals.runner.extract import build_outcome, build_timing

T0 = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def _record(**overrides):
    base = {
        "task": {
            "id": 1, "status": "awaiting_approval", "stage": "awaiting_approval",
            "error_code": None, "error_detail": None,
            "created_at": T0, "completed_at": None,
        },
        "steps": [],
        "clarifications": [],
        "usage": {"calls": 2, "input_tokens": 6000, "output_tokens": 150,
                  "cost_cny": 0.04, "model_ms": 5000},
        "version": None,
    }
    base.update(overrides)
    return base


def _step(seq, tool, *, summary=None, arguments=None, status="ok", at_s=1):
    return {"seq": seq, "tool_name": tool, "arguments": arguments,
            "result_summary": summary, "status": status, "retry_count": 0,
            "created_at": T0 + timedelta(seconds=at_s)}


def test_outcome__validation_codes_and_bodies_from_steps():
    body_v1 = {"scope": {"type": "zone", "ids": [99]}}
    body_v2 = {"scope": {"type": "zone", "ids": [1]}}
    record = _record(
        steps=[
            _step(1, "create_policy", summary={"version_id": 7}),
            _step(2, "validate_policy",
                  summary={"ok": False, "issues": [{"code": "E_UNKNOWN_ZONE"}]}),
            _step(3, "update_policy_draft",
                  summary={"version_id": 7, "previous_body": body_v1}),
            _step(4, "validate_policy", summary={"ok": True, "issues": []}),
            _step(5, "simulate_policy",
                  summary={"warnings": [{"code": "W_NO_EFFECT"}]}),
        ],
        version={"status": "awaiting_approval", "body": body_v2},
    )
    outcome = build_outcome(record)
    assert outcome.submitted
    assert outcome.validation_codes == ("E_UNKNOWN_ZONE",)
    # 中间态一条不丢: previous_body 链 + 最终版
    assert outcome.all_draft_bodies == (body_v1, body_v2)
    assert outcome.replay_warnings == ("W_NO_EFFECT",)
    assert "create_policy" in outcome.executed_tools


def test_outcome__schema_reject_read_from_error_detail():
    record = _record(task={
        "id": 1, "status": "dead_letter", "stage": "dead_letter",
        "error_code": "tool_error",
        "error_detail": "工具 create_policy 不可重试错误: InvalidPolicyBody: ...",
        "created_at": T0, "completed_at": T0 + timedelta(seconds=9),
    })
    outcome = build_outcome(record)
    assert outcome.schema_rejected and not outcome.submitted


def test_outcome__attempted_unknown_tool_from_protocol_error():
    record = _record(task={
        "id": 1, "status": "failed", "stage": "failed",
        "error_code": "model_protocol_error",
        "error_detail": "该阶段只接受 ('create_policy',), 模型给了 publish_policy",
        "created_at": T0, "completed_at": T0 + timedelta(seconds=5),
    })
    assert build_outcome(record).attempted_unknown_tools == ("publish_policy",)


def test_timing__clarify_wait_subtracted_from_wall():
    # 总墙钟 100s, 其中等人回答 60s -> 系统在跑的只有 40s (SPEC-007 第一节第 3 项)
    record = _record(
        task={
            "id": 1, "status": "awaiting_approval", "stage": "awaiting_approval",
            "error_code": None, "error_detail": None,
            "created_at": T0, "completed_at": None,
        },
        steps=[_step(1, "create_policy", summary={"version_id": 7}, at_s=100)],
        clarifications=[{
            "asked_seq": 1, "question": "哪个区?", "answer": "生鲜区",
            "missing_slots": ["scope"],
            "asked_at": T0 + timedelta(seconds=10),
            "answered_at": T0 + timedelta(seconds=70),
        }],
    )
    timing = build_timing(record)
    assert timing["wall_ms"] == 40_000
    assert timing["clarify_rounds"] == 1


def test_timing__clarifying_terminal_ends_at_ask():
    # 停在 clarifying 没人答: 终点是提问时刻, 等待不计入
    record = _record(
        task={
            "id": 1, "status": "clarifying", "stage": "clarifying",
            "error_code": None, "error_detail": None,
            "created_at": T0, "completed_at": None,
        },
        clarifications=[{
            "asked_seq": 1, "question": "?", "answer": None,
            "missing_slots": ["scope"],
            "asked_at": T0 + timedelta(seconds=8), "answered_at": None,
        }],
    )
    assert build_timing(record)["wall_ms"] == 8_000


def test_timing__repair_rounds_counted_from_transitions():
    record = _record(steps=[
        _step(1, "stage_transition", arguments={"to": "repairing"}),
        _step(2, "update_policy_draft", summary={"version_id": 7, "previous_body": {}}),
        _step(3, "stage_transition", arguments={"to": "validating"}),
        _step(4, "stage_transition", arguments={"to": "repairing"}),
    ])
    assert build_timing(record)["repair_rounds"] == 2
