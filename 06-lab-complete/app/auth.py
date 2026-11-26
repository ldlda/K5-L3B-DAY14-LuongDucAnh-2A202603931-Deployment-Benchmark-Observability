"""API-key authentication for the protected agent endpoint."""

import hmac
from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from .config import settings


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    """Dependency cho FastAPI — dùng trong /ask: Depends(verify_api_key)."""
    configured_key = settings.agent_api_key
    if not api_key or not configured_key or not hmac.compare_digest(api_key, configured_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key
