from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import get_db


logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
async def health(db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, str]:
    try:
        await db.command("ping")
    except Exception as exc:
        logger.exception("MongoDB health check failed")
        raise HTTPException(
            status_code=503,
            detail="MongoDB 不可用",
        ) from exc
    return {"status": "ok", "mongodb": "ok"}
