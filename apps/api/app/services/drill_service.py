"""演练 (drill) —— 网页上一键触发场景回放 (SPEC-005 前置 B)。

设计要点:
1. **事件流复用 packages/scenario**, 不自己解析 YAML —— 演练与 CLI 模拟器共用
   同一份场景语义 (方案 B 抽包的目的所在);
2. **投递不拼 SQL 也不 HTTP 自调**: 调与 /ingest 路由同一个 service 函数, 并复用
   同一个 pydantic 请求模型做校验 —— 演练与真机走同一份校验逻辑, 只少 HTTP 一跳;
3. **状态放内存, 不进 agent_tasks** (决策 4): 那张表有审计/重试/死信语义, 演练是
   一次性演示动作。代价是 API 重启即丢, 已写在响应的 note 字段里。
   只保留最近 drill_history_limit 次 (默认 20), 超出丢最旧 —— 内存必须有上界;
4. **同一场景不允许并发** (决策 5): 两份事件流交叉灌进同一批传感器,
   时间线会变得没法解释;
5. **中途失败记 status=failed + error**, 不允许静默消失。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scenario import Source, load_source, resolve_epoch, to_payload

from ..config import settings
from ..db import session_factory
from ..routers.ingest import IngestPayload  # 与 /ingest 同一个校验模型 (SPEC-005)
from . import ingest_service


def _detect_scenarios_dir() -> Path:
    """向上找仓库根的 scenarios/ (本机) 或 /srv/scenarios (容器, 由 Dockerfile COPY)。

    找不到就在导入时报错 —— 与"Dockerfile 漏 COPY 当场炸"同一哲学, 不留到运行时。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scenarios"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("找不到 scenarios/ 目录 (仓库根或容器 /srv 下应有一份)")


SCENARIOS_DIR: Path = _detect_scenarios_dir()

# 场景名只允许字母数字下划线连字符: 挡掉路径穿越, 非法名与不存在同栈返回 404
_SCENARIO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_MEMORY_NOTE = (
    "演练状态只保存在内存 (SPEC-005 决策 4): API 重启会丢失, "
    "且只保留最近若干次记录, 丢了重跑即可"
)


class ScenarioNotFound(Exception):
    """场景名非法或对应的 YAML 不存在 -> 404。"""


class DrillConflict(Exception):
    """同一场景已有演练在跑 -> 409 (决策 5)。"""


@dataclass(slots=True)
class Drill:
    id: str
    scenario: str
    speed: float
    events_total: int
    started_at: dt.datetime
    status: str = "running"          # running / completed / failed
    events_sent: int = 0
    finished_at: dt.datetime | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = None  # 持有引用, 防止后台任务被 GC 中断


_drills: dict[str, Drill] = {}  # 插入序即时间序 (Python dict 保序)


def public_view(drill: Drill) -> dict[str, Any]:
    return {
        "drill_id": drill.id,
        "scenario": drill.scenario,
        "speed": drill.speed,
        "status": drill.status,
        "events_total": drill.events_total,
        "events_sent": drill.events_sent,
        "started_at": drill.started_at.isoformat(),
        "finished_at": drill.finished_at.isoformat() if drill.finished_at else None,
        "error": drill.error,
        "note": _MEMORY_NOTE,
    }


def list_scenarios() -> list[dict[str, Any]]:
    """可用场景: 名字 + 事件数 + 覆盖时长 (给前端下拉框)。"""
    out: list[dict[str, Any]] = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        src = load_source(str(path), None)
        out.append({
            "scenario": path.stem,       # POST /drills/{scenario} 用这个
            "name": src.name,
            "events_total": len(src.events),
            "duration_s": src.events[-1]["at_s"] if src.events else 0,
        })
    return out


def get_drill(drill_id: str) -> Drill | None:
    return _drills.get(drill_id)


def start_drill(name: str) -> Drill:
    """校验并启动一次演练, 事件投递在后台任务里按仿真时间进行。"""
    if not _SCENARIO_NAME_RE.fullmatch(name):
        raise ScenarioNotFound(name)
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.is_file():
        raise ScenarioNotFound(name)
    if any(d.scenario == name and d.status == "running" for d in _drills.values()):
        raise DrillConflict(name)

    src = load_source(str(path), None)
    drill = Drill(
        id=uuid.uuid4().hex[:12],
        scenario=name,
        speed=settings().drill_speed,
        events_total=len(src.events),
        started_at=dt.datetime.now(dt.UTC),
    )
    _register(drill)
    drill.task = asyncio.create_task(_run(drill, src))
    return drill


def _register(drill: Drill) -> None:
    _drills[drill.id] = drill
    while len(_drills) > settings().drill_history_limit:
        # 先丢最旧的已结束记录; 全在跑 (病态场景) 才丢最旧的运行中记录
        victim = next((k for k, d in _drills.items() if d.status != "running"), None)
        del _drills[victim if victim is not None else next(iter(_drills))]


async def _run(drill: Drill, src: Source) -> None:
    """按仿真时间投递事件。语义与 sim.py 的 live 模式一致:
    epoch 取"现在"(YAML 剧本没有真实时间), time_scale=1/speed 让时间戳贴合墙上时钟,
    /status 的 age/online 判定才有意义。
    """
    factory = session_factory()
    epoch_ms = resolve_epoch(src, shift_to_now=False)
    time_scale = 1.0 / drill.speed
    t0 = time.monotonic()
    try:
        for ev in src.events:
            wait = t0 + ev["at_s"] / drill.speed - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            payload = IngestPayload(**to_payload(ev, epoch_ms, time_scale))
            # 每条事件独立事务: 中途失败不回滚已投递的事件, 与真机逐条上报一致
            async with factory() as session, session.begin():
                await ingest_service.ingest_event(session, payload.model_dump())
            drill.events_sent += 1
        drill.status = "completed"
    except Exception as e:  # 要点 5: 失败必须可见, 不允许静默消失
        drill.status = "failed"
        drill.error = f"{type(e).__name__}: {e}"
    finally:
        drill.finished_at = dt.datetime.now(dt.UTC)


def reset() -> None:
    """测试夹具用: 清空注册表。用例应自行等演练进入终态再退出, 避免后台任务外溢。"""
    _drills.clear()
