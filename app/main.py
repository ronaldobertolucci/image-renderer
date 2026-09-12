import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse

from app.config import settings
from app.schemas import (
    EnqueueResponse,
    RenderRequest,
    StatusResponse,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Redis client (shared, async)
# ---------------------------------------------------------------------------

redis_client: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa e encerra recursos compartilhados."""
    global redis_client

    settings.create_dirs()

    redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    yield

    await redis_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Image Renderer",
    description="Microsserviço assíncrono de renderização de imagens em camadas.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_redis() -> aioredis.Redis:
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Serviço Redis indisponível.")
    return redis_client


async def _get_task_state(task_id: str) -> dict:
    """Busca o estado de uma task no Redis. Lança 404 se não existir."""
    client = _get_redis()
    raw = await client.get(f"task:{task_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' não encontrada.")
    return json.loads(raw)


async def _save_task_state(task_id: str, state: dict) -> None:
    """Persiste o estado de uma task no Redis com TTL."""
    client = _get_redis()
    ttl_seconds = settings.file_ttl_hours * 3600
    await client.set(
        f"task:{task_id}",
        json.dumps(state),
        ex=ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.post(
    "/render",
    response_model=EnqueueResponse,
    status_code=202,
    summary="Enfileira uma requisição de renderização",
)
async def enqueue_render(request: RenderRequest) -> EnqueueResponse:
    """
    Recebe o payload JSON, gera um task_id e enfileira o trabalho no Celery.
    Retorna imediatamente com HTTP 202 — a renderização ocorre em background.
    """
    task_id = str(uuid.uuid4())

    # Persiste o estado inicial no Redis
    await _save_task_state(task_id, {"status": TaskStatus.processing.value})

    # Enfileira no Celery (import local evita circular import com celery_app)
    from worker.tasks import render_image_task
    render_image_task.delay(task_id, request.model_dump(mode="json"))

    return EnqueueResponse(task_id=task_id, status=TaskStatus.processing)


@app.get(
    "/status/{task_id}",
    response_model=StatusResponse,
    summary="Consulta o status de uma task",
)
async def get_status(task_id: str) -> StatusResponse:
    """
    Retorna o estado atual da task:
    - `processing` — ainda sendo processada pelo worker
    - `completed`  — imagem disponível para download
    - `failed`     — erro durante a renderização (detalhes em `error`)
    """
    state = await _get_task_state(task_id)

    return StatusResponse(
        task_id=task_id,
        status=TaskStatus(state["status"]),
        download_url=state.get("download_url"),
        expires_at=state.get("expires_at"),
        error=state.get("error"),
    )


@app.get(
    "/download/{task_id}",
    summary="Faz o download da imagem renderizada",
    responses={
        200: {"content": {"image/png": {}}},
        202: {"description": "Ainda em processamento"},
        404: {"description": "Task não encontrada"},
        410: {"description": "Arquivo expirado ou removido"},
    },
)
async def download_image(task_id: str) -> Response:
    """
    Retorna o arquivo PNG gerado.
    - HTTP 200  → imagem pronta, inicia download
    - HTTP 202  → ainda processando, tente novamente
    - HTTP 404  → task desconhecida
    - HTTP 410  → task existia mas o arquivo já foi removido pelo cleanup
    """
    state = await _get_task_state(task_id)
    status = TaskStatus(state["status"])

    if status == TaskStatus.processing:
        return Response(
            content=json.dumps({"detail": "Ainda em processamento."}),
            status_code=202,
            media_type="application/json",
        )

    if status == TaskStatus.failed:
        raise HTTPException(
            status_code=422,
            detail=f"Renderização falhou: {state.get('error', 'erro desconhecido')}",
        )

    # status == completed
    file_path = settings.output_dir / f"{task_id}.png"

    if not file_path.exists():
        raise HTTPException(
            status_code=410,
            detail="Arquivo expirado ou removido. Solicite uma nova renderização.",
        )

    return FileResponse(
        path=str(file_path),
        media_type="image/png",
        filename=f"{task_id}.png",
    )


@app.get("/health", summary="Healthcheck", include_in_schema=False)
async def health() -> dict:
    """Verifica conectividade com Redis."""
    try:
        client = _get_redis()
        await client.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis indisponível: {e}")