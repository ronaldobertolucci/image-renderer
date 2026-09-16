"""
renderer.py
-----------
Motor de renderização: canvas → camadas image / svg_image / text (com símbolos inline).

Pipeline SVG:
  _rasterize_svg()       → CairoSVG converte SVG em RGBA em memória
  _make_fill_layer()     → fill sólido ou gradiente do mesmo tamanho
  _apply_svg_transform() → máscara alpha + fill + shadow compostos corretamente
"""

import logging
import re
from io import BytesIO
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.schemas import (
    AnyLayer, AnySymbol,
    BaselineAlign, FillGradient, FillSolid,
    FitMode, ImageLayerSchema, ImageSymbolSchema,
    RenderRequest, Shadow, SvgImageLayerSchema,
    SvgSymbolSchema, SvgTransform, TextAlign, TextLayerSchema,
    VerticalAlign,
)
from worker.asset_cache import fetch_asset

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos internos para unidades de renderização de texto
# ---------------------------------------------------------------------------

class WordUnit(NamedTuple):
    text: str
    width: int

class SpaceUnit(NamedTuple):
    text: str
    width: int

class SymbolUnit(NamedTuple):
    image:           Image.Image
    width:           int              # largura total incluindo padding (symbol_spacing * 2)
    baseline_align:  BaselineAlign
    shadow_image:    Image.Image | None = None
    shadow_offset_x: int = 0
    shadow_offset_y: int = 0
    padding_x:       int = 0         # deslocamento interno para centralizar o símbolo no unit

RenderUnit = WordUnit | SpaceUnit | SymbolUnit


# ---------------------------------------------------------------------------
# Utilitários de cor e opacidade
# ---------------------------------------------------------------------------

def _hex_to_rgba(hex_color: str, opacity: float = 1.0) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, int(opacity * 255))


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _apply_opacity(image: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return image.copy()
    img = image.convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda x: int(x * opacity))
    return Image.merge("RGBA", (r, g, b, a))


def _safe_composite(canvas: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """
    Alpha-composite `layer` onto `canvas` at (x, y), lidando com coordenadas
    negativas ou que ultrapassem os limites do canvas sem lançar exceção.
    """
    cw, ch = canvas.size
    lw, lh = layer.size

    if x >= cw or y >= ch:
        return

    src_x = max(0, -x)
    src_y = max(0, -y)
    dst_x = max(0, x)
    dst_y = max(0, y)

    crop_w = min(lw - src_x, cw - dst_x)
    crop_h = min(lh - src_y, ch - dst_y)

    if crop_w <= 0 or crop_h <= 0:
        return

    cropped = layer.crop((src_x, src_y, src_x + crop_w, src_y + crop_h))
    canvas.alpha_composite(cropped, dest=(dst_x, dst_y))


# ---------------------------------------------------------------------------
# Redimensionamento de imagens (fit modes)
# ---------------------------------------------------------------------------

def _fit_image(image: Image.Image, target_w: int, target_h: int, fit: FitMode) -> Image.Image:
    if fit == FitMode.none:
        return image
    src_w, src_h = image.size
    if fit == FitMode.contain:
        ratio = min(target_w / src_w, target_h / src_h)
        return image.resize((int(src_w * ratio), int(src_h * ratio)), Image.LANCZOS)
    # cover
    ratio = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * ratio), int(src_h * ratio)
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top  = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


# ---------------------------------------------------------------------------
# Pipeline SVG
# ---------------------------------------------------------------------------

def _rasterize_svg(path: Path, width: int | None = None, height: int | None = None) -> Image.Image:
    """
    Converte um arquivo SVG em RGBA usando CairoSVG.
    width/height opcionais forçam o tamanho de saída.
    """
    import cairosvg
    kwargs: dict = {}
    if width:
        kwargs["output_width"] = width
    if height:
        kwargs["output_height"] = height
    png_bytes = cairosvg.svg2png(bytestring=path.read_bytes(), **kwargs)
    return Image.open(BytesIO(png_bytes)).convert("RGBA")


def _make_fill_layer(size: tuple[int, int], fill: FillSolid | FillGradient) -> Image.Image:
    """Cria um layer de fill (sólido ou gradiente) no tamanho dado."""
    if isinstance(fill, FillSolid):
        return Image.new("RGBA", size, _hex_to_rgba(fill.color))

    # Gradiente: interpola pixel a pixel ao longo do eixo definido
    w, h = size
    gradient = Image.new("RGB", size)
    draw = ImageDraw.Draw(gradient)

    stops = [_hex_to_rgb(c) for c in fill.colors]
    n = len(stops) - 1

    if fill.direction == "horizontal":
        length = max(w - 1, 1)
        for x in range(w):
            t = x / length
            seg = min(int(t * n), n - 1)
            local_t = t * n - seg
            c1, c2 = stops[seg], stops[seg + 1]
            color = tuple(int(c1[i] + (c2[i] - c1[i]) * local_t) for i in range(3))
            draw.line([(x, 0), (x, h - 1)], fill=color)
    else:
        length = max(h - 1, 1)
        for y in range(h):
            t = y / length
            seg = min(int(t * n), n - 1)
            local_t = t * n - seg
            c1, c2 = stops[seg], stops[seg + 1]
            color = tuple(int(c1[i] + (c2[i] - c1[i]) * local_t) for i in range(3))
            draw.line([(0, y), (w - 1, y)], fill=color)

    return gradient.convert("RGBA")


def _apply_svg_transform(svg_img: Image.Image, transform: SvgTransform) -> Image.Image:
    """
    Aplica fill (sólido ou gradiente) e shadow a uma imagem SVG rasterizada.

    Algoritmo:
    1. Extrai o canal alpha do SVG como máscara de forma.
    2. Cria o layer de fill e aplica a máscara.
    3. Se shadow configurado:
       a. Pinta a máscara com a cor da sombra.
       b. Aplica GaussianBlur.
       c. Usa canvas expandido para acomodar offset sem clip.
       d. Compõe sombra atrás, fill na frente, e recorta ao tamanho original.
    """
    size = svg_img.size
    _, _, _, alpha = svg_img.split()

    # Fill: usa cor/gradiente definido ou mantém cores originais do SVG
    if transform.fill is not None:
        fill_layer = _make_fill_layer(size, transform.fill)
        fill_layer.putalpha(alpha)
    else:
        fill_layer = svg_img.copy()

    if not transform.shadow:
        return fill_layer

    s = transform.shadow
    ox, oy = s.offset_x, s.offset_y
    blur = s.blur_radius

    # Shadow: pinta a forma com a cor da sombra e borra
    shadow_color = _hex_to_rgb(s.color)
    shadow_base = Image.new("RGBA", size, (*shadow_color, 255))
    shadow_base.putalpha(alpha)
    shadow_blurred = shadow_base.filter(ImageFilter.GaussianBlur(blur))

    # Canvas expandido para garantir que shadow não seja clipada
    pad_l = max(0, -ox) + blur
    pad_r = max(0, +ox) + blur
    pad_t = max(0, -oy) + blur
    pad_b = max(0, +oy) + blur

    tmp_w = size[0] + pad_l + pad_r
    tmp_h = size[1] + pad_t + pad_b
    tmp = Image.new("RGBA", (tmp_w, tmp_h), (0, 0, 0, 0))

    # Sombra com offset
    tmp.alpha_composite(shadow_blurred, dest=(pad_l + ox, pad_t + oy))
    # Fill na posição original
    tmp.alpha_composite(fill_layer, dest=(pad_l, pad_t))

    # Recorta de volta ao tamanho original (shadow que saiu das bordas é clipada)
    return tmp.crop((pad_l, pad_t, pad_l + size[0], pad_t + size[1]))


# ---------------------------------------------------------------------------
# Camadas de imagem (raster)
# ---------------------------------------------------------------------------

def _apply_image_layer(canvas: Image.Image, layer: ImageLayerSchema) -> None:
    path = fetch_asset(layer.url)
    img  = Image.open(path).convert("RGBA")

    target_w = layer.width  or img.width
    target_h = layer.height or img.height

    img = _fit_image(img, target_w, target_h, layer.fit)
    img = _apply_opacity(img, layer.opacity)

    _safe_composite(canvas, img, layer.x, layer.y)
    logger.debug("image layer order=%d pos=(%d,%d)", layer.order, layer.x, layer.y)


# ---------------------------------------------------------------------------
# Camadas SVG (raster + transform)
# ---------------------------------------------------------------------------

def _apply_svg_layer(canvas: Image.Image, layer: SvgImageLayerSchema) -> None:
    """
    Pipeline completo para um layer do tipo svg_image:
    rasteriza → aplica fill + shadow → compõe no canvas.

    A shadow que extrapola os limites do ícone é colocada no canvas
    separadamente, antes do ícone, com o offset configurado.
    """
    path = fetch_asset(layer.url)
    svg_img = _rasterize_svg(path, layer.width, layer.height)

    _, _, _, alpha = svg_img.split()
    s = layer.transform.shadow

    # 1. Fill com máscara (ou cores originais do SVG se fill=None)
    if layer.transform.fill is not None:
        fill_layer = _make_fill_layer(svg_img.size, layer.transform.fill)
        fill_layer.putalpha(alpha)
    else:
        fill_layer = svg_img.copy()
    fill_layer = _apply_opacity(fill_layer, layer.opacity)

    # 2. Shadow no canvas diretamente (permite que extrapole a área do ícone)
    if s:
        shadow_color = _hex_to_rgb(s.color)
        shadow_base  = Image.new("RGBA", svg_img.size, (*shadow_color, 255))
        shadow_base.putalpha(alpha)
        shadow_blurred = shadow_base.filter(ImageFilter.GaussianBlur(s.blur_radius))
        shadow_blurred = _apply_opacity(shadow_blurred, layer.opacity)
        _safe_composite(canvas, shadow_blurred, layer.x + s.offset_x, layer.y + s.offset_y)

    # 3. Fill (ícone principal) por cima
    _safe_composite(canvas, fill_layer, layer.x, layer.y)
    logger.debug("svg_image layer order=%d pos=(%d,%d)", layer.order, layer.x, layer.y)


# ---------------------------------------------------------------------------
# Parsing de tokens de texto
# ---------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"(\{[A-Za-z0-9_]+\})")


def _parse_tokens(content: str, symbols_map: dict[str, AnySymbol]) -> list[dict]:
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
    ratio = target_height / img.height
    new_w = max(1, int(img.width * ratio))
    return img.resize((new_w, target_height), Image.LANCZOS)


def _load_symbol_image(
    schema: AnySymbol,
    ascent: int,
) -> tuple[Image.Image, BaselineAlign, Image.Image | None, int, int]:
    """
    Baixa e processa um símbolo, retornando fill e sombra separadamente.

    A sombra é retornada como imagem independente para ser composta no canvas
    ANTES do fill, sem clipping pelos limites do símbolo.

    Retorna: (fill_img, baseline_align, shadow_img | None, shadow_ox, shadow_oy)
    """
    target_h = schema.height if schema.height else ascent
    path     = fetch_asset(schema.url)

    if isinstance(schema, ImageSymbolSchema):
        img = Image.open(path).convert("RGBA")
        img = _scale_symbol(img, target_h)
        return img, schema.baseline_align, None, 0, 0

    # SvgSymbolSchema
    img = _rasterize_svg(path, height=target_h)

    shadow_img: Image.Image | None = None
    shadow_ox = shadow_oy = 0

    if schema.transform:
        _, _, _, alpha = img.split()

        # Fill: recolore ou mantém cores originais
        if schema.transform.fill is not None:
            fill_layer = _make_fill_layer(img.size, schema.transform.fill)
            fill_layer.putalpha(alpha)
            img = fill_layer

        # Shadow com canvas expandido para evitar clipping por blur e offset
        if schema.transform.shadow:
            s = schema.transform.shadow
            _, _, _, alpha_for_shadow = img.split()

            # Padding: acomoda blur (em todos os lados) + offset (no lado oposto)
            pad_l = max(0, -s.offset_x) + s.blur_radius
            pad_r = max(0,  s.offset_x) + s.blur_radius
            pad_t = max(0, -s.offset_y) + s.blur_radius
            pad_b = max(0,  s.offset_y) + s.blur_radius

            padded_w = img.width  + pad_l + pad_r
            padded_h = img.height + pad_t + pad_b

            # Coloca a máscara alpha no centro do canvas expandido
            padded_alpha = Image.new("L", (padded_w, padded_h), 0)
            padded_alpha.paste(alpha_for_shadow, (pad_l, pad_t))

            shadow_color_rgb = _hex_to_rgb(s.color)
            shadow_base = Image.new("RGBA", (padded_w, padded_h), (*shadow_color_rgb, 255))
            shadow_base.putalpha(padded_alpha)
            shadow_blurred = shadow_base.filter(ImageFilter.GaussianBlur(s.blur_radius))

            # Aplica opacity da sombra boost no canal alpha
            if s.opacity < 1.0:
                sr, sg, sb, sa = shadow_blurred.split()
                sa = sa.point(lambda v: int(v * s.opacity))
                shadow_blurred = Image.merge("RGBA", (sr, sg, sb, sa))

            shadow_img = shadow_blurred

            # Offset de renderização: posiciona o canvas expandido corretamente
            # O conteúdo do shadow está em (pad_l, pad_t) no canvas expandido.
            # Queremos que a sombra apareça em (offset_x, offset_y) do símbolo.
            # → canvas_topleft = symbol_pos + (offset_x - pad_l, offset_y - pad_t)
            shadow_ox = s.offset_x - pad_l
            shadow_oy = s.offset_y - pad_t

    # img já foi rasterizado em target_h; _scale_symbol é no-op ou correção fina
    img = _scale_symbol(img, target_h)

    return img, schema.baseline_align, shadow_img, shadow_ox, shadow_oy


def _build_render_units(
    tokens:         list[dict],
    font:           ImageFont.FreeTypeFont,
    symbol_images:  dict[str, tuple],
    word_spacing:   int = 0,
    symbol_spacing: int = 0,
) -> list[RenderUnit]:
    units: list[RenderUnit] = []
    for token in tokens:
        if token["type"] == "symbol":
            img, align, shadow_img, shadow_ox, shadow_oy = symbol_images[token["key"]]
            # symbol_spacing adiciona margem em cada lado do símbolo
            total_w = img.width + symbol_spacing * 2
            units.append(SymbolUnit(
                image=img, width=total_w, baseline_align=align,
                shadow_image=shadow_img, shadow_offset_x=shadow_ox, shadow_offset_y=shadow_oy,
                padding_x=symbol_spacing,
            ))
        else:
            for subpart in re.findall(r"\S+|\s+", token["content"]):
                w = _text_width(subpart, font)
                if subpart.strip() == "":
                    # word_spacing aumenta/diminui o espaço entre palavras
                    units.append(SpaceUnit(text=subpart, width=max(0, w + word_spacing)))
                else:
                    units.append(WordUnit(text=subpart, width=w))
    return units


# ---------------------------------------------------------------------------
# Quebra de linha
# ---------------------------------------------------------------------------

def _build_lines(units: list[RenderUnit], max_width: int | None) -> list[list[RenderUnit]]:
    if max_width is None:
        return [units]

    lines: list[list[RenderUnit]] = []
    current: list[RenderUnit]     = []
    current_w = 0

    for unit in units:
        if not current and isinstance(unit, SpaceUnit):
            continue
        if current_w + unit.width > max_width and current:
            while current and isinstance(current[-1], SpaceUnit):
                current_w -= current[-1].width
                current.pop()
            if current:
                lines.append(current)
            current   = [] if isinstance(unit, SpaceUnit) else [unit]
            current_w = 0  if isinstance(unit, SpaceUnit) else unit.width
        else:
            current.append(unit)
            current_w += unit.width

    while current and isinstance(current[-1], SpaceUnit):
        current.pop()
    if current:
        lines.append(current)

    return lines


# ---------------------------------------------------------------------------
# Renderização de uma linha
# ---------------------------------------------------------------------------

def _line_width(line: list[RenderUnit]) -> int:
    return sum(u.width for u in line)


def _render_line(
    canvas:     Image.Image,
    draw:       ImageDraw.ImageDraw,
    line:       list[RenderUnit],
    layer:      TextLayerSchema,
    line_y:     int,
    font:       ImageFont.FreeTypeFont,
    color_rgba: tuple,
    line_h:     int,
    ascent:     int,
) -> None:
    lw     = _line_width(line)
    max_w  = layer.max_width

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
            sym_h = unit.image.height
            if unit.baseline_align == BaselineAlign.top:
                sym_y = line_y
            elif unit.baseline_align == BaselineAlign.baseline:
                sym_y = line_y + ascent - sym_h
            else:
                sym_y = line_y + (line_h - sym_h) // 2

            sym_y_int = max(0, int(sym_y))
            sym_x_int = int(x) + unit.padding_x  # padding_x = symbol_spacing

            # Sombra composta ANTES do fill, sem clipping pelos limites do símbolo
            if unit.shadow_image is not None:
                shadow = _apply_opacity(unit.shadow_image, layer.opacity)
                _safe_composite(
                    canvas, shadow,
                    sym_x_int + unit.shadow_offset_x,
                    sym_y_int + unit.shadow_offset_y,
                )

            sym = _apply_opacity(unit.image.convert("RGBA"), layer.opacity)
            _safe_composite(canvas, sym, sym_x_int, sym_y_int)
            x += unit.width  # avança pela largura total (imagem + padding dos dois lados)


# ---------------------------------------------------------------------------
# Camada de texto
# ---------------------------------------------------------------------------

def _normalize_content(content: str, tab_size: int) -> str:
    """
    Normaliza sequências de escape do conteúdo de texto:
    - \r\n e \r  → \n  (quebras de linha Windows/Mac antigo)
    - \t          → N espaços (tab_size espaços)
    Newlines e tabs reais (vindos do JSON) já chegam processados pelo parser.
    """
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = content.replace("\t", " " * tab_size)
    return content


def _measure_text_block(
    paragraphs:    list[str],
    font:          ImageFont.FreeTypeFont,
    symbol_images: dict[str, tuple],
    layer:         TextLayerSchema,
    line_h:        int,
) -> int:
    """
    Calcula a altura total do bloco de texto em pixels.
    """
    ascent, descent = font.getmetrics()
    
    # CORREÇÃO:
    # Substituímos `ascent + descent` por apenas `ascent`.
    # Isso faz a centralização ignorar o espaço do descent fantasma,
    # mantendo o texto perfeitamente centralizado usando a linha de base.
    visual_last_line_h = ascent 

    total = 0
    last_p_idx = len(paragraphs) - 1

    for p_idx, paragraph in enumerate(paragraphs):
        tokens = _parse_tokens(paragraph, {})
        units  = _build_render_units(tokens, font, symbol_images,
                                     layer.word_spacing, layer.symbol_spacing)
        lines  = _build_lines(units, layer.max_width)
        n = 1 if not lines else len(lines)

        if p_idx < last_p_idx:
            # Parágrafos intermediários: todas as linhas usam line_h
            total += n * line_h + layer.paragraph_spacing
        else:
            # Último parágrafo: última linha usa altura baseada no ascent (sem descent)
            total += (n - 1) * line_h + visual_last_line_h

    return total


def _compute_start_y(layer: TextLayerSchema, block_height: int) -> int:
    """
    Calcula o y inicial do bloco considerando vertical_align e max_height.
    Se max_height nao definido ou vertical_align=top, retorna layer.y sem alteracao.
    """
    if not layer.max_height or layer.vertical_align == VerticalAlign.top:
        return layer.y
    slack = layer.max_height - block_height
    if slack <= 0:
        return layer.y  # bloco maior que a caixa: sem ajuste
    if layer.vertical_align == VerticalAlign.center:
        return layer.y + slack // 2
    # bottom
    return layer.y + slack


def _apply_text_layer(
    canvas:      Image.Image,
    layer:       TextLayerSchema,
    symbols_map: dict[str, AnySymbol],
) -> None:
    font_path = fetch_asset(layer.font_url)
    font      = ImageFont.truetype(str(font_path), size=layer.font_size)

    ascent, _ = font.getmetrics()
    line_h    = int(layer.font_size * layer.line_height)

    # Normaliza escapes e divide em parágrafos pelo \n
    normalized = _normalize_content(layer.content, layer.tab_size)
    paragraphs = normalized.split("\n")

    # Pré-carrega símbolos usados em qualquer parágrafo
    symbol_images: dict[str, tuple] = {}
    used_keys = set(re.findall(r"\{[A-Za-z0-9_]+\}", normalized))
    for key in used_keys:
        symbol_images[key] = _load_symbol_image(symbols_map[key], ascent)

    # Centralização vertical: mede o bloco e ajusta o y inicial
    block_h   = _measure_text_block(paragraphs, font, symbol_images, layer, line_h)
    current_y = _compute_start_y(layer, block_h)

    overlay    = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw       = ImageDraw.Draw(overlay)
    color_rgba = _hex_to_rgba(layer.color, layer.opacity)
    total_lines = 0

    for p_idx, paragraph in enumerate(paragraphs):
        tokens = _parse_tokens(paragraph, symbols_map)
        units  = _build_render_units(tokens, font, symbol_images, layer.word_spacing, layer.symbol_spacing)
        lines  = _build_lines(units, layer.max_width)

        if not lines:
            current_y += line_h
        else:
            for line in lines:
                _render_line(overlay, draw, line, layer, current_y, font, color_rgba, line_h, ascent)
                current_y += line_h
            total_lines += len(lines)

        if p_idx < len(paragraphs) - 1:
            current_y += layer.paragraph_spacing

    canvas.alpha_composite(overlay)
    logger.debug(
        "text layer order=%d paragraphs=%d lines=%d block_h=%dpx start_y=%d",
        layer.order, len(paragraphs), total_lines, block_h,
        _compute_start_y(layer, block_h),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render(request: RenderRequest, output_path: Path) -> None:
    """Renderiza a imagem completa e salva em output_path."""
    logger.info(
        "Iniciando renderizacao: %dx%d, %d camadas",
        request.canvas.width, request.canvas.height, len(request.layers),
    )

    bg     = _hex_to_rgba(request.canvas.background_color)
    canvas = Image.new("RGBA", (request.canvas.width, request.canvas.height), bg)

    for layer in request.layers:
        if isinstance(layer, ImageLayerSchema):
            _apply_image_layer(canvas, layer)
        elif isinstance(layer, SvgImageLayerSchema):
            _apply_svg_layer(canvas, layer)
        elif isinstance(layer, TextLayerSchema):
            _apply_text_layer(canvas, layer, request.symbols_map)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(str(output_path), format="PNG", optimize=True)
    logger.info("Renderizacao concluida: %s", output_path)