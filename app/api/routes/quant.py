from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.repositories.published import PublishedRepository
from app.db.session import get_connection

router = APIRouter(tags=["quant"])


@router.get("/predictions/upcoming")
async def predictions_upcoming(limit: int = Query(50, ge=1, le=200), conn: AsyncConnection = Depends(get_connection)) -> dict:
    repo = PublishedRepository(conn)
    rows = await repo.fetch_all("select * from published_match_predictions order by as_of desc limit :limit", {"limit": limit})
    return {"ok": True, "data": {"predictions": rows}}


@router.get("/ev/opportunities")
async def ev_opportunities(limit: int = Query(50, ge=1, le=200), conn: AsyncConnection = Depends(get_connection)) -> dict:
    rows = await PublishedRepository(conn).ev_opportunities(limit)
    return {"ok": True, "data": {"opportunities": rows}}


@router.get("/ev/blocked")
async def ev_blocked(limit: int = Query(50, ge=1, le=200), conn: AsyncConnection = Depends(get_connection)) -> dict:
    rows = await PublishedRepository(conn).blocked_decisions(limit)
    return {"ok": True, "data": {"blocked": rows}}


@router.get("/calibration/summary")
async def calibration_summary(limit: int = Query(50, ge=1, le=200), conn: AsyncConnection = Depends(get_connection)) -> dict:
    rows = await PublishedRepository(conn).calibration_summary(limit)
    return {"ok": True, "data": {"calibration": rows}}


@router.get("/model/diagnostics")
async def model_diagnostics(conn: AsyncConnection = Depends(get_connection)) -> dict:
    rows = await PublishedRepository(conn).model_diagnostics()
    return {"ok": True, "data": {"models": rows}}
