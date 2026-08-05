from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class Pagination:
    """Validated page parameters shared by list endpoints."""

    page: int
    page_size: int

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size


def get_pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Pagination:
    return Pagination(page=page, page_size=page_size)


def get_db(request: Request) -> AsyncIOMotorDatabase:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="MongoDB 不可用")
    return database
