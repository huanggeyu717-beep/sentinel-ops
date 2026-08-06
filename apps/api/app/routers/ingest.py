"""事件入口 —— 取代原 IoT Rules -> Lambda 链路。

接收 device-sim(或未来的真实 MQTT bridge) 的原格式消息, 规范化后写入
waterlevel_readings / rfid_scans / device_heartbeats 并推进 sensorstate。
幂等键: (device_id, 业务标识, ts)。W3 起在 service 层追加 Policy 引擎投递。
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import ingest_service

router = APIRouter(tags=["ingest"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class IngestPayload(BaseModel):
    kind: Literal["sensor_state", "heartbeat", "rfid_scan"]
    device_id: str = Field(min_length=1, max_length=64)
    ts: int = Field(gt=0, description="事件发生时间, epoch 毫秒")
    sensor_id: int | None = None
    zone_id: int | None = None
    state: str | None = None
    value: int | None = None
    rfid_uid: str | None = None
    rfid_id: str | None = None
    uptime_ms: int | None = None

    @model_validator(mode="after")
    def _require_fields_per_kind(self) -> "IngestPayload":
        """每类事件的必填字段在入口就挡掉, 不让脏数据进 service。"""
        if self.kind == "sensor_state" and self.sensor_id is None:
            raise ValueError("sensor_state 事件必须带 sensor_id")
        if self.kind == "rfid_scan" and not self.rfid_uid:
            raise ValueError("rfid_scan 事件必须带 rfid_uid")
        return self


class IngestResponse(BaseModel):
    ok: bool
    kind: str
    stored: bool
    state_updated: bool
    # W2 事故联动 (SPEC-003): sensor_state 带 incident_id, rfid_scan 另带 matched/reason
    incident_id: int | None = None
    matched: bool | None = None
    reason: str | None = None


@router.post("/ingest", response_model=IngestResponse, response_model_exclude_none=True)
async def ingest(payload: IngestPayload, session: SessionDep) -> IngestResponse:
    result = await ingest_service.ingest_event(session, payload.model_dump())
    return IngestResponse(
        ok=True, kind=result.kind, stored=result.stored, state_updated=result.state_updated,
        incident_id=result.incident_id, matched=result.matched, reason=result.reason,
    )


class BatchIngestResponse(BaseModel):
    ok: bool
    accepted: int
    stored: int


@router.post("/ingest/batch", response_model=BatchIngestResponse)
async def ingest_batch(payloads: list[IngestPayload], session: SessionDep) -> BatchIngestResponse:
    """批量入口: 历史数据回放时逐条 POST 太慢, 一次提交一批(同一事务)。"""
    if len(payloads) > 1000:
        raise HTTPException(status_code=413, detail="单批最多 1000 条")
    stored = 0
    for p in payloads:
        result = await ingest_service.ingest_event(session, p.model_dump())
        stored += int(result.stored)
    return BatchIngestResponse(ok=True, accepted=len(payloads), stored=stored)
