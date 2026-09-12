from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "image_renderer",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.tasks"]
)

celery_app.conf.update(
    # Serialização
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Confiabilidade: confirma a task só após execução, não no recebimento
    task_acks_late=True,

    # Um prefetch por worker — crítico para controlar RAM com imagens grandes
    worker_prefetch_multiplier=1,

    # Concorrência definida via settings (default 4)
    worker_concurrency=settings.celery_concurrency,

    # Permite consultar se a task foi iniciada (status STARTED)
    task_track_started=True,

    # Resultados no backend expiram junto com o TTL dos arquivos
    result_expires=settings.file_ttl_hours * 3600,

    # Fuso horário para o Beat
    timezone="UTC",
    enable_utc=True,

    # Agendamento do Beat
    beat_schedule={
        "cleanup-old-files": {
            "task": "worker.tasks.cleanup_old_files",
            "schedule": crontab(minute=0),  # todo início de hora
        },
    },
)