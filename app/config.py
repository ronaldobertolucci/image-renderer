from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Armazenamento
    output_dir: Path = Path("/tmp/generated")
    asset_cache_dir: Path = Path("/tmp/asset_cache")

    # Ciclo de vida dos arquivos
    file_ttl_hours: int = 24

    # Celery
    celery_concurrency: int = 4

    # Download de assets externos
    asset_download_timeout_seconds: int = 15
    asset_download_max_retries: int = 3

    # Canvas padrão (pode ser sobrescrito pelo payload)
    default_canvas_width: int = 1992
    default_canvas_height: int = 2770

    def create_dirs(self) -> None:
        """Garante que os diretórios necessários existam."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.asset_cache_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()