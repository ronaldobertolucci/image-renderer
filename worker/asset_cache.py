"""
asset_cache.py
--------------
Download e cache local de assets externos (imagens e fontes).

Design:
- Cada URL é mapeada para um arquivo em disco via hash SHA-256 da URL.
- A extensão original é preservada para que o Pillow identifique o formato.
- A escrita é atômica (tmp → rename) para evitar arquivos corrompidos
  caso dois workers tentem baixar o mesmo asset simultaneamente.
- Retry com backoff exponencial para falhas de rede transitórias.
"""

import hashlib
import logging
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _normalize_url(url: object) -> str:
    """Converte str ou Pydantic HttpUrl para string pura."""
    return str(url)


def _cache_path_for(url: str) -> Path:
    """
    Retorna o caminho de cache para uma URL.

    O nome do arquivo é: <sha256[:24]><extensão_original>
    Exemplos:
      https://cdn.com/Roboto.ttf   → /tmp/asset_cache/a3f9c2b1d4e7...ttf
      https://cdn.com/shield.png   → /tmp/asset_cache/7bc3a1e2f094...png
    """
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
    suffix = Path(urlparse(url).path).suffix.lower() or ".bin"
    return settings.asset_cache_dir / f"{url_hash}{suffix}"


def _write_atomic(dest: Path, data: bytes) -> None:
    """
    Escreve `data` em `dest` de forma atômica usando um arquivo temporário
    no mesmo diretório. Evita que workers concorrentes leiam arquivos parciais.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=dest.parent,
        delete=False,
        suffix=".tmp",
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(data)

    # rename é atômico em sistemas POSIX; no Windows pode sobrescrever
    tmp_path.replace(dest)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def fetch_asset(url: object) -> Path:
    """
    Retorna o caminho local do asset, fazendo download se necessário.

    Parâmetros
    ----------
    url : str | pydantic.HttpUrl
        URL do asset externo.

    Retorna
    -------
    Path
        Caminho absoluto do arquivo em cache.

    Lança
    -----
    RuntimeError
        Se todas as tentativas de download falharem.
    """
    url_str = _normalize_url(url)
    cache_path = _cache_path_for(url_str)

    if cache_path.exists():
        logger.debug("Cache hit: %s → %s", url_str, cache_path.name)
        return cache_path

    logger.info("Baixando asset: %s", url_str)
    return _download_with_retry(url_str, cache_path)


def _download_with_retry(url: str, dest: Path) -> Path:
    """
    Tenta baixar `url` até `settings.asset_download_max_retries` vezes
    com backoff exponencial entre tentativas.
    """
    max_retries = settings.asset_download_max_retries
    timeout = settings.asset_download_timeout_seconds
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                headers={"User-Agent": "image-renderer/1.0"},
            ) as client:
                response = client.get(url)
                response.raise_for_status()

            _write_atomic(dest, response.content)
            logger.info(
                "Download concluído: %s (%.1f KB, tentativa %d/%d)",
                dest.name,
                len(response.content) / 1024,
                attempt,
                max_retries,
            )
            return dest

        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_error = exc
            wait = 2 ** attempt  # 2s, 4s, 8s...

            if attempt < max_retries:
                logger.warning(
                    "Falha ao baixar %s (tentativa %d/%d): %s. "
                    "Aguardando %ds antes de tentar novamente.",
                    url, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Todas as %d tentativas falharam para: %s",
                    max_retries, url,
                )

    raise RuntimeError(
        f"Não foi possível baixar o asset após {max_retries} tentativas: {url}"
    ) from last_error


def clear_cache() -> int:
    """
    Remove todos os arquivos do cache local.
    Retorna o número de arquivos removidos.
    Útil para testes e manutenção.
    """
    removed = 0
    for f in settings.asset_cache_dir.glob("*"):
        if f.is_file():
            f.unlink()
            removed += 1
    logger.info("Cache limpo: %d arquivo(s) removido(s).", removed)
    return removed