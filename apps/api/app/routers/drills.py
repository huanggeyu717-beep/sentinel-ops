"""演练触发接口 —— SPEC-005 前置 B。

前端一律不自己造事件: 直接调 /ingest 发假数据会绕过模拟器, 破坏
"模拟器是唯一事实源"。业务全在 services/drill_service, 这里只做 HTTP 拼装。
查看用 read 权限即可; 触发要 operator 及以上 (决策 6), viewer 403。
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..services import drill_service
from ..services.auth_service import PERM_READ, PERM_TRIGGER_DRILL
from .auth import require_permission

router = APIRouter(prefix="/drills", tags=["drills"])


# 注意声明顺序: /scenarios 必须先于 /{drill_id}, 否则会被当成一个 drill_id 匹配掉
@router.get("/scenarios", dependencies=[Depends(require_permission(PERM_READ))])
async def scenarios() -> dict[str, Any]:
    return {"ok": True, "scenarios": drill_service.list_scenarios()}


@router.post(
    "/{scenario}",
    status_code=202,
    dependencies=[Depends(require_permission(PERM_TRIGGER_DRILL))],
)
async def start(scenario: str) -> dict[str, Any]:
    try:
        drill = drill_service.start_drill(scenario)
    except drill_service.ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"场景不存在: {scenario}") from None
    except drill_service.DrillConflict:
        raise HTTPException(
            status_code=409,
            detail=f"场景 {scenario} 已有演练在跑, 跑完再来 (SPEC-005 决策 5)",
        ) from None
    return {"ok": True, **drill_service.public_view(drill)}


@router.get("/{drill_id}", dependencies=[Depends(require_permission(PERM_READ))])
async def drill_status(drill_id: str) -> dict[str, Any]:
    drill = drill_service.get_drill(drill_id)
    if drill is None:
        raise HTTPException(
            status_code=404,
            detail="演练不存在或记录已被丢弃 (状态在内存: API 重启丢失, 只留最近若干次)",
        )
    return {"ok": True, **drill_service.public_view(drill)}
