from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class Repository:
    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = await self.conn.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        result = await self.conn.execute(text(sql), params or {})
        row = result.first()
        return dict(row._mapping) if row else None

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        await self.conn.execute(text(sql), params or {})

