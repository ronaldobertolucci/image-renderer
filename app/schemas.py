from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums compartilhados
# ---------------------------------------------------------------------------

class BaselineAlign(str, Enum):
    center   = "center"
    top      = "top"
    baseline = "baseline"

class FitMode(str, Enum):
    cover   = "cover"
    contain = "contain"
    none    = "none"

class TextAlign(str, Enum):
    left   = "left"
    center = "center"
    right  = "right"

class TaskStatus(str, Enum):
    processing = "processing"
    completed  = "completed"
    failed     = "failed"


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

class CanvasSchema(BaseModel):
    width:            int = Field(default=1992, gt=0, le=8000)
    height:           int = Field(default=2770, gt=0, le=8000)
    background_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# SVG Transform
# ---------------------------------------------------------------------------

class FillSolid(BaseModel):
    type:  Literal["solid"]
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class FillGradient(BaseModel):
    type:      Literal["gradient"]
    direction: Literal["horizontal", "vertical"]
    colors:    list[str] = Field(min_length=2, max_length=10)

    @field_validator("colors")
    @classmethod
    def validate_colors(cls, v: list[str]) -> list[str]:
        pattern = re.compile(r"^#[0-9A-Fa-f]{6}$")
        for c in v:
            if not pattern.match(c):
                raise ValueError(f"Cor de gradiente invalida: '{c}'. Use #RRGGBB.")
        return v


AnyFill = Annotated[
    Union[FillSolid, FillGradient],
    Field(discriminator="type"),
]


class Shadow(BaseModel):
    color:       str   = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    blur_radius: int   = Field(default=4,   ge=0,    le=100)
    offset_x:   int   = Field(default=2,   ge=-500, le=500)
    offset_y:   int   = Field(default=2,   ge=-500, le=500)
    opacity:    float = Field(default=1.0, ge=0.0,  le=1.0,
                              description="Intensidade da sombra. 1.0 = opaco, 0.0 = invisível.")


class SvgTransform(BaseModel):
    fill:   Optional[AnyFill] = None  # None = mantém cores originais do SVG
    shadow: Optional[Shadow] = None


    @model_validator(mode="after")
    def validate_at_least_one(self) -> "SvgTransform":
        if self.fill is None and self.shadow is None:
            raise ValueError("transform deve ter ao menos fill ou shadow.")
        return self

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

class ImageSymbolSchema(BaseModel):
    type:           Literal["image"]
    url:            HttpUrl
    baseline_align: BaselineAlign = BaselineAlign.center
    height:         Optional[int] = Field(default=None, gt=0, description="Altura em px. Se omitido, usa o ascent da fonte.")


class SvgSymbolSchema(BaseModel):
    type:           Literal["svg_image"]
    url:            HttpUrl
    baseline_align: BaselineAlign = BaselineAlign.center
    height:         Optional[int] = Field(default=None, gt=0, description="Altura em px. Se omitido, usa o ascent da fonte.")
    transform:      Optional[SvgTransform] = None


AnySymbol = Annotated[
    Union[ImageSymbolSchema, SvgSymbolSchema],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

class ImageLayerSchema(BaseModel):
    order:   int   = Field(ge=0)
    type:    Literal["image"]
    url:     HttpUrl
    x:       int
    y:       int
    width:   Optional[int]   = Field(default=None, gt=0)
    height:  Optional[int]   = Field(default=None, gt=0)
    opacity: float           = Field(default=1.0, ge=0.0, le=1.0)
    fit:     FitMode         = FitMode.none


class SvgImageLayerSchema(BaseModel):
    order:     int   = Field(ge=0)
    type:      Literal["svg_image"]
    url:       HttpUrl
    x:         int
    y:         int
    width:     Optional[int]   = Field(default=None, gt=0)
    height:    Optional[int]   = Field(default=None, gt=0)
    opacity:   float           = Field(default=1.0, ge=0.0, le=1.0)
    transform: SvgTransform


class TextLayerSchema(BaseModel):
    order:       int   = Field(ge=0)
    type:        Literal["text"]
    content:     str   = Field(min_length=1)
    font_url:    HttpUrl
    font_size:   int   = Field(gt=0, le=1000)
    color:       str   = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    x:           int
    y:           int
    max_width:         Optional[int] = Field(default=None, gt=0)
    line_height:       float        = Field(default=1.2, ge=0.5, le=5.0)
    paragraph_spacing: int          = Field(default=0, ge=0,
                                            description="Pixels extras entre paragrafos (separados por \\n).")
    tab_size:          int          = Field(default=4, ge=1, le=32,
                                            description="Espacos equivalentes por \\t.")
    text_align:        TextAlign    = TextAlign.left
    opacity:           float        = Field(default=1.0, ge=0.0, le=1.0)


AnyLayer = Annotated[
    Union[ImageLayerSchema, SvgImageLayerSchema, TextLayerSchema],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Request principal
# ---------------------------------------------------------------------------

_SYMBOL_KEY_RE = re.compile(r"^\{[A-Za-z0-9_]+\}$")
_SYMBOL_USE_RE = re.compile(r"\{[A-Za-z0-9_]+\}")


class RenderRequest(BaseModel):
    callback_url: Optional[HttpUrl]    = None
    canvas:       CanvasSchema         = Field(default_factory=CanvasSchema)
    symbols_map:  dict[str, AnySymbol] = Field(default_factory=dict)
    layers:       list[AnyLayer]       = Field(min_length=1)

    @field_validator("symbols_map")
    @classmethod
    def validate_symbol_keys(cls, v: dict) -> dict:
        for key in v:
            if not _SYMBOL_KEY_RE.match(key):
                raise ValueError(f"Chave invalida: '{key}'. Use o formato {{NOME}}.")
        return v

    @model_validator(mode="after")
    def validate_symbols_referenced(self) -> RenderRequest:
        if not self.symbols_map:
            return self
        defined = set(self.symbols_map.keys())
        for layer in self.layers:
            if isinstance(layer, TextLayerSchema):
                used = set(_SYMBOL_USE_RE.findall(layer.content))
                undefined = used - defined
                if undefined:
                    raise ValueError(
                        f"Simbolos usados mas nao definidos no symbols_map: {undefined}"
                    )
        return self

    @model_validator(mode="after")
    def sort_layers_by_order(self) -> RenderRequest:
        self.layers = sorted(self.layers, key=lambda l: l.order)
        return self


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class EnqueueResponse(BaseModel):
    task_id: str
    status:  TaskStatus = TaskStatus.processing


class StatusResponse(BaseModel):
    task_id:      str
    status:       TaskStatus
    download_url: Optional[str] = None
    expires_at:   Optional[str] = None
    error:        Optional[str] = None