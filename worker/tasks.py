"""
tasks.py
--------
Tasks Celery do microsserviço de renderização.

- render_image_task  : renderiza uma imagem e atualiza o estado no Redis.
- cleanup_old_files  : remove arquivos gerados mais velhos que o TTL configurado.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import redis

from app.config import settings
from app.schemas import RenderRequest, TaskStatus
from worker.celery_app import celery_app
from worker.renderer import render

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis síncrono (Celery workers rodam em threads, não em event loop)
# ---------------------------------------------------------------------------

_redis = redis.from_url(settings.redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _update_status(task_id: str, state: dict) -> None:
    """Persiste o estado da task no Redis com TTL."""
    ttl = settings.file_ttl_hours * 3600
    _redis.set(f"task:{task_id}", json.dumps(state), ex=ttl)


def _notify_callback(callback_url: str, payload: dict) -> None:
    """
    Faz POST no callback_url com o resultado da task.
    Falhas são logadas mas não propagadas — o webhook é best-effort.
    """
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(callback_url, json=payload)
            response.raise_for_status()
            logger.info("Callback entregue: %s → HTTP %d", callback_url, response.status_code)
    except Exception as exc:
        logger.warning("Callback falhou para %s: %s", callback_url, exc)


# ---------------------------------------------------------------------------
# Task: renderização
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="worker.tasks.render_image_task",
    max_retries=0,      # sem retries automáticos — o asset_cache já lida com isso
    ignore_result=True, # não precisamos do resultado no backend, gerenciamos no Redis
)
def render_image_task(self, task_id: str, payload: dict) -> None:
    """
    Renderiza a imagem descrita em `payload` e salva em disco.

    Fluxo:
    1. Reconstrói o RenderRequest a partir do JSON.
    2. Chama render() que compõe as camadas com Pillow.
    3. Atualiza o estado no Redis para `completed` ou `failed`.
    4. Se houver callback_url, notifica o cliente.
    """
    logger.info("Iniciando task: %s", task_id)

    try:
        request = RenderRequest(**payload)
        output_path = settings.output_dir / f"{task_id}.png"

        render(request, output_path)

        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=settings.file_ttl_hours)
        ).isoformat()

        completed_state = {
            "status": TaskStatus.completed.value,
            "download_url": f"/download/{task_id}",
            "expires_at": expires_at,
        }
        _update_status(task_id, completed_state)
        logger.info("Task concluída: %s → %s", task_id, output_path.name)

        # Notifica callback se presente
        if request.callback_url:
            _notify_callback(
                str(request.callback_url),
                {"task_id": task_id, **completed_state},
            )

    except Exception as exc:
        error_msg = str(exc)
        logger.exception("Falha na task %s: %s", task_id, error_msg)

        _update_status(task_id, {
            "status": TaskStatus.failed.value,
            "error": error_msg,
        })

        # Re-raise para o Celery registrar a task como FAILURE no backend
        raise


# ---------------------------------------------------------------------------
# Task: limpeza de arquivos antigos (executada pelo Beat a cada hora)
# ---------------------------------------------------------------------------

@celery_app.task(name="worker.tasks.cleanup_old_files")
def cleanup_old_files() -> dict:
    """
    Remove arquivos PNG gerados há mais de `file_ttl_hours` horas.

    Retorna um dict com o resultado da limpeza para o log do Beat.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.file_ttl_hours)
    removed = 0
    errors = 0

    output_dir: Path = settings.output_dir
    if not output_dir.exists():
        logger.info("Diretório de saída não existe ainda, nada a limpar.")
        return {"removed": 0, "errors": 0}

    for file in output_dir.glob("*.png"):
        try:
            mtime = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                file.unlink()
                removed += 1
                logger.debug("Removido: %s (criado em %s)", file.name, mtime.isoformat())
        except Exception as exc:
            logger.warning("Erro ao remover %s: %s", file.name, exc)
            errors += 1

    logger.info(
        "Cleanup concluído: %d arquivo(s) removido(s), %d erro(s). "
        "Corte em: %s",
        removed, errors, cutoff.isoformat(),
    )
    return {"removed": removed, "errors": errors}