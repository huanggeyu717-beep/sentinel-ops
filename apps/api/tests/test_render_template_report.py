"""render_template_report (SPEC-008 第六节并排对照的"模板那一份"): 确定性、
全覆盖按序、零落库。脚本经 subprocess 真实执行 (与 test_reset_script 同一理由):
"逐字节相同"只有对着真实 stdout 的字节才断言得到。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from test_agent_helpers import clean_agent_tables, db  # noqa: F401
from test_report_task_service import T0, insert_incident

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "dev" / "render_template_report.py"


def make_incident_with_events() -> int:
    async def go(conn):
        incident_id = await insert_incident(
            conn,
            acknowledged_by_employee_id=2,
            acknowledged_at=T0 + timedelta(minutes=12),
        )
        for kind, at in (
            ("opened", T0),
            ("acknowledged", T0 + timedelta(minutes=12)),
            ("sensor_dry", T0 + timedelta(minutes=50)),
            ("resolved", T0 + timedelta(hours=1)),
        ):
            await conn.execute(
                "INSERT INTO incident_events (incident_id, kind, actor, at) "
                "VALUES ($1, $2, 'system', $3)", incident_id, kind, at,
            )
        return incident_id

    return db(go)


def run_script(incident_id: int) -> subprocess.CompletedProcess[bytes]:
    # 不带 text=True: 确定性断言在字节上, 不经过一层解码
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(incident_id)],
        cwd=REPO_ROOT, env=os.environ.copy(), capture_output=True, timeout=60,
    )


def test_template_report__byte_identical_across_runs(client):
    """钉死"确定性"本身: 同一条事故跑两次, stdout 逐字节相同。"""
    incident_id = make_incident_with_events()
    first = run_script(incident_id)
    second = run_script(incident_id)
    assert first.returncode == 0, first.stderr.decode()
    assert second.returncode == 0, second.stderr.decode()
    assert first.stdout == second.stdout
    assert first.stdout  # 相同但为空不算过


def test_template_report__every_fact_one_line_in_pack_order(client, svc):
    """模板的定义: 不做取舍 —— 事实包里每条 (含缺失的"无此记录") 恰好一行,
    顺序与事实包一致。"""
    from app.services import report_task_service

    incident_id = make_incident_with_events()

    async def load(factory):
        async with factory() as session:
            return await report_task_service.load_fact_pack(session, incident_id)

    fact_pack = svc(load)
    out = run_script(incident_id).stdout.decode()
    assert out.splitlines() == [f"{f['label']}: {f['text']}。" for f in fact_pack]
    # 这条事故未派单: 缺失条目也铺开了, 不是被跳过
    assert "派单给: 无此记录。" in out


def test_template_report__writes_no_incident_reports_row(client):
    incident_id = make_incident_with_events()

    async def count(conn) -> int:
        return await conn.fetchval("SELECT count(*) FROM incident_reports")

    before = db(count)
    assert run_script(incident_id).returncode == 0
    assert db(count) == before == 0


def test_template_report__unknown_incident_exits_nonzero(client):
    result = run_script(999999)
    assert result.returncode == 1
    assert "不存在" in result.stderr.decode()
