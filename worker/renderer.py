"""
renderer.py
-----------
Motor de renderização: recebe um RenderRequest já validado e produz
um arquivo PNG no caminho especificado.

Fluxo principal:
  render()
    ├── _apply_image_layer()   → alpha_composite de imagens
    └── _apply_text_layer()
          ├── _parse_tokens()      → divide conteúdo em texto/símbolos
          ├── _build_render_units() → transforma tokens em unidades com largura
          ├── _build_lines()       → quebra de linha respeitando max_width
          └── _render_line()       → renderiza cada linha (texto + símbolos)
"""

import logging
import re
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

from app.schemas import (
    AnyLayer,
    BaselineAlign,
    FitMode,
    ImageLayerSchema,
    RenderRequest,
    SymbolSchema,
    TextAlign,
    TextLayerSchema,
)
from worker.asset_cache import fetch_asset

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

class WordUnit(NamedTuple):
    text: str
    width: int

class SpaceUnit(NamedTuple):
    text: str
    width: int

class SymbolUnit(NamedTuple):
    image: Image.Image
    width: int
    baseline_align: BaselineAlign

RenderUnit = WordUnit | SpaceUnit | SymbolUnit


# ---------------------------------------------------------------------------
# Utilitários de cor e opacidade
# ---------------------------------------------------------------------------

def _hex_to_rgba(hex_color: str, opacity: float = 1.0) -> tuple[int, int, int, int]:
    """Converte '#RRGGBB' + opacidade para tupla RGBA."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, int(opacity * 255))


def _apply_opacity(image: Image.Image, opacity: float) -> Image.Image:
    """Retorna uma cópia da imagem com o canal alfa multiplicado por opacity."""
    if opacity >= 1.0:
        return image.copy()

    img = image.convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda x: int(x * opacity))
    return Image.merge("RGBA", (r, g, b, a))


# ---------------------------------------------------------------------------
# Redimensionamento de imagens (fit modes)
# ---------------------------------------------------------------------------

def _fit_image(
    image: Image.Image,
    target_w: int,
    target_h: int,
    fit: FitMode,
) -> Image.Image:
    """
    Redimensiona `image` para (target_w, target_h) conforme o modo:
    - cover:   preenche o alvo cortando o excesso (mantém proporção)
    - contain: cabe inteiro dentro do alvo sem cortar
    - none:    retorna sem alteração
    """
    if fit == FitMode.none:
        return image

    src_w, src_h = image.size

    if fit == FitMode.contain:
        ratio = min(target_w / src_w, target_h / src_h)
        new_w, new_h = int(src_w * ratio), int(src_h * ratio)
        return image.resize((new_w, new_h), Image.LANCZOS)

    # cover
    ratio = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * ratio), int(src_h * ratio)
    resized = image.resize((new_w, new_h), Image.LANCZOS)

    # Centraliza e recorta
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


# ---------------------------------------------------------------------------
# Camadas de imagem
# ---------------------------------------------------------------------------

def _apply_image_layer(canvas: Image.Image, layer: ImageLayerSchema) -> None:
    """Baixa, redimensiona e compõe uma camada de imagem no canvas."""
    path = fetch_asset(layer.url)
    img = Image.open(path).convert("RGBA")

    target_w = layer.width or img.width
    target_h = layer.height or img.height

    img = _fit_image(img, target_w, target_h, layer.fit)
    img = _apply_opacity(img, layer.opacity)

    canvas.alpha_composite(img, dest=(layer.x, layer.y))
    logger.debug("Imagem aplicada: order=%d, pos=(%d,%d)", layer.order, layer.x, layer.y)


# ---------------------------------------------------------------------------
# Parsing de tokens de texto
# ---------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"(\{[A-Za-z0-9_]+\})")


def _parse_tokens(content: str, symbols_map: dict[str, SymbolSchema]) -> list[dict]:
    """
    Divide o conteúdo em tokens de texto e símbolos.

    Exemplo:
      "Ataque {S2} com Força!" → [
        {"type": "text",   "content": "Ataque "},
        {"type": "symbol", "key": "{S2}"},
        {"type": "text",   "content": " com Força!"},
      ]
    """
    tokens = []
    for part in _SYMBOL_RE.split(content):
        if not part:
            continue
        if part in symbols_map:
            tokens.append({"type": "symbol", "key": part})
        else:
            tokens.append({"type": "text", "content": part})
    return tokens


# ---------------------------------------------------------------------------
# Construção de unidades de renderização
# ---------------------------------------------------------------------------

def _text_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    """Largura em pixels de uma string com a fonte dada."""
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _scale_symbol(img: Image.Image, target_height: int) -> Image.Image:
    """Redimensiona símbolo mantendo proporção, com altura = target_height."""
    ratio = target_height / img.height
    new_w = max(1, int(img.width * ratio))
    return img.resize((new_w, target_height), Image.LANCZOS)


def _build_render_units(
    tokens: list[dict],
    font: ImageFont.FreeTypeFont,
    symbol_images: dict[str, tuple[Image.Image, BaselineAlign]],
) -> list[RenderUnit]:
    """
    Converte tokens em unidades atômicas de renderização com largura pré-calculada.
    Texto é dividido em palavras e espaços para permitir quebra de linha.
    """
    units: list[RenderUnit] = []

    for token in tokens:
        if token["type"] == "symbol":
            img, align = symbol_images[token["key"]]
            units.append(SymbolUnit(image=img, width=img.width, baseline_align=align))

        else:
            # Divide em sequências de não-espaço e espaço
            subparts = re.findall(r"\S+|\s+", token["content"])
            for subpart in subparts:
                w = _text_width(subpart, font)
                if subpart.strip() == "":
                    units.append(SpaceUnit(text=subpart, width=w))
                else:
                    units.append(WordUnit(text=subpart, width=w))

    return units


# ---------------------------------------------------------------------------
# Quebra de linha (word-wrap)
# ---------------------------------------------------------------------------

def _build_lines(
    units: list[RenderUnit],
    max_width: int | None,
) -> list[list[RenderUnit]]:
    """
    Agrupa unidades em linhas respeitando max_width.
    Espaços no início e no final de cada linha são descartados.
    Se max_width for None, retorna uma única linha com tudo.
    """
    if max_width is None:
        return [units]

    lines: list[list[RenderUnit]] = []
    current: list[RenderUnit] = []
    current_w = 0

    for unit in units:
        # Descarta espaços no início de uma nova linha
        if not current and isinstance(unit, SpaceUnit):
            continue

        if current_w + unit.width > max_width and current:
            # Remove espaços finais antes de fechar a linha
            while current and isinstance(current[-1], SpaceUnit):
                current_w -= current[-1].width
                current.pop()
            if current:
                lines.append(current)
            # Inicia nova linha (descartando espaços)
            if isinstance(unit, SpaceUnit):
                current, current_w = [], 0
            else:
                current, current_w = [unit], unit.width
        else:
            current.append(unit)
            current_w += unit.width

    # Última linha
    while current and isinstance(current[-1], SpaceUnit):
        current.pop()
    if current:
        lines.append(current)

    return lines


# ---------------------------------------------------------------------------
# Renderização de uma linha
# ---------------------------------------------------------------------------

def _line_pixel_width(line: list[RenderUnit]) -> int:
    return sum(u.width for u in line)


def _render_line(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    line: list[RenderUnit],
    layer: TextLayerSchema,
    line_y: int,
    font: ImageFont.FreeTypeFont,
    color_rgba: tuple,
    line_h: int,
    ascent: int,
) -> None:
    """
    Renderiza uma única linha de unidades no canvas.
    Gerencia alinhamento horizontal e vertical dos símbolos inline.
    """
    lw = _line_pixel_width(line)
    max_w = layer.max_width

    # Posição X inicial conforme alinhamento
    if layer.text_align == TextAlign.center and max_w:
        x = layer.x + (max_w - lw) // 2
    elif layer.text_align == TextAlign.right and max_w:
        x = layer.x + max_w - lw
    else:
        x = layer.x

    for unit in line:
        if isinstance(unit, (WordUnit, SpaceUnit)):
            draw.text((x, line_y), unit.text, font=font, fill=color_rgba)
            x += unit.width

        elif isinstance(unit, SymbolUnit):
            img = unit.image
            sym_h = img.height

            # Alinhamento vertical do símbolo na linha
            if unit.baseline_align == BaselineAlign.top:
                sym_y = line_y
            elif unit.baseline_align == BaselineAlign.baseline:
                # Alinha a base do símbolo com a linha de base do texto
                sym_y = line_y + ascent - sym_h
            else:
                # center: centraliza na altura total da linha
                sym_y = line_y + (line_h - sym_h) // 2

            sym_y = max(0, sym_y)  # garante que não saia do canvas pelo topo

            # Compõe o símbolo com opacidade da camada
            sym = _apply_opacity(img.convert("RGBA"), layer.opacity)
            canvas.alpha_composite(sym, dest=(int(x), int(sym_y)))
            x += unit.width


# ---------------------------------------------------------------------------
# Camada de texto
# ---------------------------------------------------------------------------

def _apply_text_layer(
    canvas: Image.Image,
    layer: TextLayerSchema,
    symbols_map: dict[str, SymbolSchema],
) -> None:
    """Renderiza uma camada de texto (com possíveis símbolos inline) no canvas."""

    # Carrega fonte
    font_path = fetch_asset(layer.font_url)
    font = ImageFont.truetype(str(font_path), size=layer.font_size)

    ascent, descent = font.getmetrics()
    line_h = int(layer.font_size * layer.line_height)

    # Carrega e escala as imagens de símbolos usados nesta camada
    symbol_images: dict[str, tuple[Image.Image, BaselineAlign]] = {}
    symbol_pattern = re.compile(r"\{[A-Za-z0-9_]+\}")
    used_keys = set(symbol_pattern.findall(layer.content))

    for key in used_keys:
        schema = symbols_map[key]
        path = fetch_asset(schema.url)
        img = Image.open(path).convert("RGBA")
        img = _scale_symbol(img, ascent)  # escala para a altura do ascent
        symbol_images[key] = (img, schema.baseline_align)

    # Parsing e layout
    tokens = _parse_tokens(layer.content, symbols_map)
    units = _build_render_units(tokens, font, symbol_images)
    lines = _build_lines(units, layer.max_width)

    # Cria draw overlay com opacidade para o texto
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    color_rgba = _hex_to_rgba(layer.color, layer.opacity)

    for i, line in enumerate(lines):
        line_y = layer.y + i * line_h
        _render_line(
            overlay, draw, line, layer,
            line_y, font, color_rgba, line_h, ascent,
        )

    canvas.alpha_composite(overlay)
    logger.debug(
        "Texto aplicado: order=%d, linhas=%d, pos=(%d,%d)",
        layer.order, len(lines), layer.x, layer.y,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render(request: RenderRequest, output_path: Path) -> None:
    """
    Renderiza a imagem completa a partir do RenderRequest e salva em output_path.

    As camadas já vêm ordenadas pelo schema (sort_layers_by_order).
    """
    logger.info(
        "Iniciando renderização: %dx%d, %d camadas",
        request.canvas.width, request.canvas.height, len(request.layers),
    )

    bg_color = _hex_to_rgba(request.canvas.background_color)
    canvas = Image.new("RGBA", (request.canvas.width, request.canvas.height), bg_color)

    for layer in request.layers:
        if isinstance(layer, ImageLayerSchema):
            _apply_image_layer(canvas, layer)
        elif isinstance(layer, TextLayerSchema):
            _apply_text_layer(canvas, layer, request.symbols_map)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(str(output_path), format="PNG", optimize=True)

    logger.info("Renderização concluída: %s", output_path)