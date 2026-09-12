from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BaselineAlign(str, Enum):
    """Alinhamento vertical do símbolo inline em relação à linha de texto."""
    center = "center"      # centralizado na altura total da linha
    top = "top"            # alinhado ao topo da linha
    baseline = "baseline"  # alinhado à base tipográfica


class FitMode(str, Enum):
    """Modo de redimensionamento para camadas de imagem."""
    cover = "cover"      # preenche o espaço, cortando o excesso
    contain = "contain"  # cabe inteiro sem cortar, mantendo proporção
    none = "none"        # usa o tamanho original da imagem


class TextAlign(str, Enum):
    left = "left"
    center = "center"
    right = "right"


class TaskStatus(str, Enum):
    processing = "processing"
    completed = "completed"
    failed = "failed"


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

class CanvasSchema(BaseModel):
    width: int = Field(default=1992, gt=0, le=8000)
    height: int = Field(default=2770, gt=0, le=8000)
    background_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

class SymbolSchema(BaseModel):
    url: HttpUrl
    baseline_align: BaselineAlign = BaselineAlign.center


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

class ImageLayerSchema(BaseModel):
    order: int = Field(ge=0)
    type: Literal["image"]
    url: HttpUrl
    x: int
    y: int
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    fit: FitMode = FitMode.none


class TextLayerSchema(BaseModel):
    order: int = Field(ge=0)
    type: Literal["text"]
    content: str = Field(min_length=1)
    font_url: HttpUrl
    font_size: int = Field(gt=0, le=1000)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    x: int
    y: int
    max_width: Optional[int] = Field(default=None, gt=0)
    line_height: float = Field(default=1.2, ge=0.5, le=5.0)
    text_align: TextAlign = TextAlign.left
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)


# Union discriminada pelo campo "type"
AnyLayer = Annotated[
    Union[ImageLayerSchema, TextLayerSchema],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Request principal
# ---------------------------------------------------------------------------

class RenderRequest(BaseModel):
    callback_url: Optional[HttpUrl] = None
    canvas: CanvasSchema = Field(default_factory=CanvasSchema)
    symbols_map: dict[str, SymbolSchema] = Field(default_factory=dict)
    layers: list[AnyLayer] = Field(min_length=1)

    @field_validator("symbols_map")
    @classmethod
    def validate_symbol_keys(cls, v: dict) -> dict:
        """Garante que as chaves sigam o padrão {KEY}."""
        pattern = re.compile(r"^\{[A-Za-z0-9_]+\}$")
        for key in v:
            if not pattern.match(key):
                raise ValueError(
                    f"Chave de símbolo inválida: '{key}'. "
                    "Use o formato {NOME}, ex: {S1}, {SHIELD}."
                )
        return v

    @model_validator(mode="after")
    def validate_symbols_referenced_in_layers(self) -> RenderRequest:
        """
        Verifica se todos os símbolos usados nos textos existem no symbols_map.
        Evita erros silenciosos durante a renderização.
        """
        if not self.symbols_map:
            return self

        symbol_pattern = re.compile(r"\{[A-Za-z0-9_]+\}")
        defined_keys = set(self.symbols_map.keys())

        for layer in self.layers:
            if isinstance(layer, TextLayerSchema):
                used = set(symbol_pattern.findall(layer.content))
                undefined = used - defined_keys
                if undefined:
                    raise ValueError(
                        f"Símbolos usados no texto mas não definidos no symbols_map: "
                        f"{undefined}. Conteúdo: '{layer.content}'"
                    )
        return self

    @model_validator(mode="after")
    def sort_layers_by_order(self) -> RenderRequest:
        """Ordena as camadas pela propriedade 'order' antes de processar."""
        self.layers = sorted(self.layers, key=lambda layer: layer.order)
        return self


# ---------------------------------------------------------------------------
# Responses da API
# ---------------------------------------------------------------------------

class EnqueueResponse(BaseModel):
    """Resposta do POST /render — retornado com HTTP 202."""
    task_id: str
    status: TaskStatus = TaskStatus.processing


class StatusResponse(BaseModel):
    """Resposta do GET /status/{task_id}."""
    task_id: str
    status: TaskStatus
    download_url: Optional[str] = None
    expires_at: Optional[str] = None  # ISO 8601
    error: Optional[str] = None