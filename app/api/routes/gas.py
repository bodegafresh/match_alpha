from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.security import require_internal_key
from app.db.session import get_connection
from app.jobs.orchestrator import JobOrchestrator

router = APIRouter(prefix="/gas", tags=["gas"])


@router.post("/callback/status")
async def gas_callback_status(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).gas_callback_status(payload)
