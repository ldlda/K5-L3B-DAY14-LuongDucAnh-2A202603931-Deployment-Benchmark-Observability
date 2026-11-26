"""Production-ready mock AI agent for the Day 14 final project."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import time

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .auth import verify_api_key
from .cost_guard import check_budget, record_cost, release_budget
from .rate_limiter import check_rate_limit
from .redis_client import r, redis_healthy
from utils.mock_llm import ask as llm_ask


class JsonFormatter(logging.Formatter):
    """Format every application log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
        }
        message = record.getMessage()
        try:
            structured = json.loads(message)
        except (TypeError, ValueError):
            payload["event"] = message
        else:
            if isinstance(structured, dict):
                payload.update(structured)
            else:
                payload["event"] = message
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = False
logger.handlers.clear()
log_handler = logging.StreamHandler()
log_handler.setFormatter(JsonFormatter())
logger.addHandler(log_handler)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Let Uvicorn finish in-flight work, then close this worker's Redis pool."""
    yield
    try:
        r.close()
    except Exception:
        logger.exception(json.dumps({"event": "redis_close_failed"}))


app = FastAPI(title="Production AI Agent", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


@app.get("/health")
def health():
    """Liveness check: process health does not depend on Redis."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness check: the agent needs Redis for shared request guards."""
    if not redis_healthy():
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return {"status": "ready"}


@app.post("/ask")
def ask(body: AskRequest, api_key: str = Depends(verify_api_key)):
    started = time.perf_counter()
    check_rate_limit(api_key)
    input_tokens = len(body.question.split())
    reserved_cost = check_budget(input_tokens=input_tokens)
    try:
        answer = llm_ask(body.question)
    except Exception:
        release_budget(reserved_cost)
        raise
    record_cost(
        input_tokens=input_tokens,
        output_tokens=len(answer.split()),
        reserved_cost=reserved_cost,
    )
    logger.info(json.dumps({
        "event": "request",
        "path": "/ask",
        "status": 200,
        "ms": round((time.perf_counter() - started) * 1000, 3),
    }))
    return {"question": body.question, "answer": answer}
