# Image Renderer

Microsserviço assíncrono para renderização de imagens de alta resolução a partir de um payload JSON. Empilha camadas de imagens raster, vetores SVG e texto com suporte a símbolos inline, gradientes, sombras e quebra de linha automática.

---

## Sumário

- [Visão Geral](#visão-geral)
- [Stack Tecnológica](#stack-tecnológica)
- [Estrutura do Projeto](#estrutura-do-projeto)
- [Instalação e Execução](#instalação-e-execução)
- [Variáveis de Ambiente](#variáveis-de-ambiente)
- [Fluxo de Execução](#fluxo-de-execução)
- [Referência da API](#referência-da-api)
- [Referência do Payload](#referência-do-payload)
- [Exemplos](#exemplos)

---

## Visão Geral

O serviço recebe um payload JSON descrevendo um canvas e suas camadas, enfileira o trabalho via Celery e retorna a imagem PNG gerada assim que o processamento for concluído. O cliente faz polling no endpoint de status ou recebe notificação via webhook.

**Características principais:**

- Composição de camadas raster (`image`) e vetoriais (`svg_image`) com controle de opacidade, posição e redimensionamento
- Renderização de SVGs externos com recoloração em tempo de execução via fill sólido ou gradiente multi-stop
- Sombras (drop shadow) com blur gaussiano e offset configurável
- Texto com quebra de linha automática, alinhamento e símbolos inline (imagem ou SVG dinâmico no meio do texto)
- Cache local de assets externos (fontes, imagens, SVGs) para evitar downloads repetidos
- Limpeza automática de arquivos gerados via Celery Beat

---

## Stack Tecnológica

| Componente | Tecnologia |
|---|---|
| API Web | FastAPI + Uvicorn |
| Fila e Broker | Redis |
| Workers | Celery |
| Agendamento | Celery Beat |
| Renderização Raster | Pillow (PIL) |
| Rasterização SVG | CairoSVG |
| Validação | Pydantic v2 |
| HTTP Client | HTTPX |

---

## Estrutura do Projeto

```
image-renderer/
├── app/
│   ├── __init__.py
│   ├── config.py        # Configurações via variáveis de ambiente
│   ├── main.py          # FastAPI: rotas /render, /status, /download, /health
│   └── schemas.py       # Modelos Pydantic do payload e responses
├── worker/
│   ├── __init__.py
│   ├── celery_app.py    # Instância Celery + agendamento do Beat
│   ├── tasks.py         # Tasks: render_image_task, cleanup_old_files
│   ├── renderer.py      # Motor de renderização (Pillow + CairoSVG)
│   └── asset_cache.py   # Download e cache local de assets externos
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Instalação e Execução

### Com Docker (recomendado)

```bash
# 1. Clone o repositório
git clone <repo-url> && cd image-renderer

# 2. Configure o ambiente
cp .env.example .env

# 3. Suba todos os serviços
docker compose up -d --build

# 4. Verifique se está saudável
curl http://localhost:8000/health
```

Quatro containers são iniciados: `redis`, `api`, `worker` e `beat`.

### Localmente (desenvolvimento)

**Pré-requisitos:** Python 3.12+, Redis rodando, bibliotecas do sistema para CairoSVG.

```bash
# Dependências de sistema (Debian/Ubuntu)
sudo apt-get install libcairo2 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0

# Ambiente Python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Redis (se não tiver instalado)
docker run -d -p 6379:6379 redis:7-alpine

# Copie e edite o .env
cp .env.example .env
# Ajuste REDIS_URL=redis://localhost:6379/0
```

Abra três terminais a partir da raiz do projeto:

```bash
# Terminal 1 — API
uvicorn app.main:app --reload --port 8000

# Terminal 2 — Worker
celery -A worker.celery_app worker --loglevel=info --concurrency=4

# Terminal 3 — Beat (limpeza agendada)
celery -A worker.celery_app beat --loglevel=info
```

> **Windows:** adicione `--pool=solo` ao comando do worker.

### PyCharm

Crie três Run Configurations do tipo **Python**:

| Nome | Módulo / Script | Parâmetros |
|---|---|---|
| FastAPI | `uvicorn` (script) | `app.main:app --reload --port 8000` |
| Celery Worker | `celery` (módulo) | `-A worker.celery_app worker --loglevel=info --concurrency=4` |
| Celery Beat | `celery` (módulo) | `-A worker.celery_app beat --loglevel=info` |

---

## Variáveis de Ambiente

Copie `.env.example` para `.env` e ajuste conforme o servidor.

| Variável | Padrão | Descrição |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | URL de conexão ao Redis |
| `OUTPUT_DIR` | `/tmp/generated` | Diretório dos PNGs gerados |
| `ASSET_CACHE_DIR` | `/tmp/asset_cache` | Cache local de assets externos |
| `FILE_TTL_HOURS` | `24` | Tempo de vida dos arquivos gerados (horas) |
| `CELERY_CONCURRENCY` | `4` | Workers Celery simultâneos |
| `ASSET_DOWNLOAD_TIMEOUT_SECONDS` | `15` | Timeout por request de asset |
| `ASSET_DOWNLOAD_MAX_RETRIES` | `3` | Tentativas com backoff exponencial |

> **Dimensionamento de `CELERY_CONCURRENCY`:** cada worker mantém uma imagem RGBA de ~22 MB em memória durante a renderização. A fórmula sugerida é `RAM disponível em MB / 80`, arredondado para baixo.

---

## Fluxo de Execução

```
Cliente
  │
  ├─ POST /render ──────────────────────────────────────────────────────┐
  │   Valida payload (Pydantic)                                         │
  │   Gera task_id (UUID)                                               │
  │   Salva status "processing" no Redis                                │
  │   Enfileira render_image_task no Celery                             │
  │   Retorna HTTP 202 { task_id, status: "processing" }               │
  │                                                                     │
  ├─ GET /status/{task_id} (polling)              Celery Worker         │
  │   Lê estado do Redis ◄────────────────── Resolve assets (cache)    │
  │   Retorna status atual                         Cria canvas RGBA     │
  │                                                Aplica camadas       │
  ├─ GET /download/{task_id}                       Salva PNG em disco   │
  │   Serve o arquivo PNG                          Atualiza Redis ──────┘
  │   quando status = "completed"                  POST callback_url (opcional)
  │
  └─ Celery Beat (a cada hora)
      Remove arquivos com mais de FILE_TTL_HOURS
```

**Estados possíveis no Redis:**

| Status | Descrição |
|---|---|
| `processing` | Task enfileirada ou em execução |
| `completed` | PNG disponível para download |
| `failed` | Erro durante renderização (detalhes em `error`) |

---

## Referência da API

### `POST /render`

Enfileira uma renderização. Retorna **HTTP 202** imediatamente.

**Request:** `Content-Type: application/json` — ver [Referência do Payload](#referência-do-payload).

**Response:**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing"
}
```

---

### `GET /status/{task_id}`

Consulta o estado atual da task.

**Response — processing:**
```json
{ "task_id": "...", "status": "processing" }
```

**Response — completed:**
```json
{
  "task_id": "...",
  "status": "completed",
  "download_url": "/download/550e8400-...",
  "expires_at": "2025-08-25T10:00:00+00:00"
}
```

**Response — failed:**
```json
{
  "task_id": "...",
  "status": "failed",
  "error": "Não foi possível baixar o asset após 3 tentativas: https://..."
}
```

---

### `GET /download/{task_id}`

Retorna o arquivo PNG gerado.

| HTTP | Situação |
|---|---|
| `200` | Arquivo pronto — inicia download |
| `202` | Ainda processando |
| `404` | Task não encontrada |
| `410` | Arquivo expirado e removido pelo cleanup |
| `422` | Renderização falhou |

---

### `GET /health`

Verifica conectividade com o Redis. Útil para healthchecks do Docker e load balancers.

```json
{ "status": "ok", "redis": "connected" }
```

---

## Referência do Payload

### Estrutura raiz

```json
{
  "callback_url": "https://seu-servidor.com/webhook",
  "canvas": { ... },
  "symbols_map": { ... },
  "layers": [ ... ]
}
```

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `callback_url` | string (URL) | Não | Webhook chamado ao concluir (POST com o resultado) |
| `canvas` | objeto | Não | Configuração do canvas (padrão: 1992×2770 branco) |
| `symbols_map` | objeto | Não | Mapa de símbolos inline para camadas de texto |
| `layers` | array | **Sim** | Lista de camadas a compor (mínimo 1) |

---

### Canvas

```json
"canvas": {
  "width": 1992,
  "height": 2770,
  "background_color": "#FFFFFF"
}
```

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `width` | int | `1992` | Largura em pixels (máx. 8000) |
| `height` | int | `2770` | Altura em pixels (máx. 8000) |
| `background_color` | string | `#FFFFFF` | Cor de fundo em hex `#RRGGBB` |

---

### Symbols Map

Mapa de chaves (`{NOME}`) para símbolos usados dentro de camadas de texto.

#### Símbolo tipo `image`

```json
"{ICONE}": {
  "type": "image",
  "url": "https://exemplo.com/shield.png",
  "baseline_align": "center",
  "height": 100
}
```

#### Símbolo tipo `svg_image`

```json
"{FOGO}": {
  "type": "svg_image",
  "url": "https://exemplo.com/fire.svg",
  "baseline_align": "center",
  "height": 120,
  "transform": {
    "fill": {
      "type": "gradient",
      "direction": "vertical",
      "colors": ["#FFA500", "#FF0000"]
    },
    "shadow": {
      "color": "#000000",
      "blur_radius": 3,
      "offset_x": 2,
      "offset_y": 2
    }
  }
}
```

**Campos comuns a ambos os tipos:**

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `type` | `"image"` \| `"svg_image"` | — | Tipo do símbolo |
| `url` | string (URL) | — | URL do asset externo |
| `baseline_align` | `"center"` \| `"top"` \| `"baseline"` | `"center"` | Alinhamento vertical na linha de texto |
| `height` | int | `null` | Altura em px. Se omitido, usa o ascent da fonte |

**Campos exclusivos de `svg_image`:**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `transform` | objeto | Não | Fill e shadow aplicados ao SVG |
| `transform.fill` | objeto | **Sim** (se transform presente) | Cor ou gradiente para recolorir o SVG |
| `transform.shadow` | objeto | Não | Sombra projetada |

---

### Layers

Todas as camadas têm `order` (inteiro, define a ordem de composição, menor = mais ao fundo) e `type`.

#### Layer tipo `image`

```json
{
  "order": 1,
  "type": "image",
  "url": "https://exemplo.com/fundo.png",
  "x": 0,
  "y": 0,
  "width": 1992,
  "height": 2770,
  "opacity": 1.0,
  "fit": "cover"
}
```

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `url` | string (URL) | — | URL da imagem raster |
| `x`, `y` | int | — | Posição no canvas |
| `width`, `height` | int | Tamanho original | Dimensões alvo |
| `opacity` | float 0–1 | `1.0` | Opacidade da camada |
| `fit` | `"cover"` \| `"contain"` \| `"none"` | `"none"` | Modo de redimensionamento |

---

#### Layer tipo `svg_image`

```json
{
  "order": 2,
  "type": "svg_image",
  "url": "https://exemplo.com/icone.svg",
  "x": 500,
  "y": 800,
  "width": 200,
  "height": 200,
  "opacity": 1.0,
  "transform": {
    "fill": {
      "type": "solid",
      "color": "#FF5733"
    },
    "shadow": {
      "color": "#000000",
      "blur_radius": 15,
      "offset_x": 10,
      "offset_y": 10
    }
  }
}
```

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `url` | string (URL) | — | URL do arquivo SVG |
| `x`, `y` | int | — | Posição no canvas |
| `width`, `height` | int | Tamanho natural do SVG | Dimensões de rasterização |
| `opacity` | float 0–1 | `1.0` | Opacidade da camada |
| `transform` | objeto | — | **Obrigatório.** Fill e shadow |

---

#### Layer tipo `text`

```json
{
  "order": 3,
  "type": "text",
  "content": "Ataque {S2} com Força total!",
  "font_url": "https://exemplo.com/Roboto.ttf",
  "font_size": 120,
  "color": "#000000",
  "x": 100,
  "y": 500,
  "max_width": 1792,
  "line_height": 1.2,
  "paragraph_spacing": 20,
  "tab_size": 4,
  "word_spacing": 8,
  "symbol_spacing": 12,
  "text_align": "left",
  "opacity": 1.0
}
```

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `content` | string | — | Texto com suporte a chaves `{SIMBOLO}` |
| `font_url` | string (URL) | — | URL do arquivo de fonte TrueType (.ttf) |
| `font_size` | int | — | Tamanho da fonte em pixels |
| `color` | string | — | Cor do texto em hex `#RRGGBB` |
| `x`, `y` | int | — | Posição do bloco de texto no canvas |
| `max_width` | int | `null` | Largura máxima em px para quebra de linha automática |
| `line_height` | float | `1.2` | Multiplicador de entrelinha (1.2 = 120% do font_size) |
| `paragraph_spacing` | int | `0` | Pixels extras adicionados entre parágrafos (separados por `\n`) |
| `tab_size` | int | `4` | Quantidade de espaços equivalentes por `\t` |
| `word_spacing` | int | `0` | Pixels extras entre palavras. Negativo aproxima, positivo afasta |
| `symbol_spacing` | int | `0` | Pixels de margem em cada lado de um símbolo inline |
| `text_align` | `"left"` \| `"center"` \| `"right"` | `"left"` | Alinhamento horizontal (requer `max_width`) |
| `opacity` | float 0–1 | `1.0` | Opacidade do texto e dos símbolos inline |

---

### Sequências de Escape em Texto

O campo `content` das camadas de texto suporta as seguintes sequências:

| Sequência | Efeito |
|---|---|
| `\n` | Quebra de linha / novo parágrafo |
| `\t` | Tabulação horizontal (equivalente a `tab_size` espaços) |
| `\r\n` | Quebra de linha Windows (normalizada automaticamente para `\n`) |
| `\\` | Barra invertida literal |

**Parágrafos consecutivos** — dois `\n` seguidos criam uma linha em branco entre blocos de texto.

**Exemplo:**
```json
{
  "type": "text",
  "content": "Título\n\nPrimeiro parágrafo.\nSegunda linha.\n\n\tTexto indentado.",
  "paragraph_spacing": 24,
  "tab_size": 4,
  "font_size": 48
}
```

---

### Transform (Fill e Shadow)

#### Fill sólido

```json
"fill": {
  "type": "solid",
  "color": "#FF5733"
}
```

#### Fill gradiente

```json
"fill": {
  "type": "gradient",
  "direction": "vertical",
  "colors": ["#FFA500", "#FF0000"]
}
```

| Campo | Tipo | Descrição |
|---|---|---|
| `direction` | `"horizontal"` \| `"vertical"` | Direção do gradiente |
| `colors` | array de strings | Lista de 2–10 cores em hex. Interpoladas em ordem |

#### Shadow

```json
"shadow": {
  "color": "#000000",
  "blur_radius": 10,
  "offset_x": 8,
  "offset_y": 8
}
```

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `color` | string | — | Cor da sombra em hex |
| `blur_radius` | int 0–100 | `4` | Raio do desfoque gaussiano |
| `offset_x` | int | `2` | Deslocamento horizontal (positivo = direita) |
| `offset_y` | int | `2` | Deslocamento vertical (positivo = baixo) |

---

## Exemplos

### Teste rápido via cURL

```bash
# 1. Enfileira a renderização
curl -s -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -d @payload.json

# 2. Consulta o status
curl -s http://localhost:8000/status/{task_id}

# 3. Baixa a imagem quando completed
curl -s http://localhost:8000/download/{task_id} -o resultado.png
```

### Payload de exemplo

```json
{
  "canvas": {
    "width": 800,
    "height": 600,
    "background_color": "#1a1a2e"
  },
  "symbols_map": {
    "{STAR}": {
      "type": "svg_image",
      "url": "https://upload.wikimedia.org/wikipedia/commons/2/29/Gold_Star.svg",
      "baseline_align": "center",
      "height": 80,
      "transform": {
        "fill": {
          "type": "gradient",
          "direction": "vertical",
          "colors": ["#FFD700", "#FFA500"]
        },
        "shadow": {
          "color": "#000000",
          "blur_radius": 4,
          "offset_x": 2,
          "offset_y": 2
        }
      }
    }
  },
  "layers": [
    {
      "order": 1,
      "type": "image",
      "url": "https://picsum.photos/seed/bg/800/600",
      "x": 0,
      "y": 0,
      "width": 800,
      "height": 600,
      "opacity": 0.35,
      "fit": "cover"
    },
    {
      "order": 2,
      "type": "svg_image",
      "url": "https://upload.wikimedia.org/wikipedia/commons/2/29/Gold_Star.svg",
      "x": 600,
      "y": 50,
      "width": 150,
      "height": 150,
      "transform": {
        "fill": {
          "type": "solid",
          "color": "#FF5733"
        },
        "shadow": {
          "color": "#000000",
          "blur_radius": 12,
          "offset_x": 8,
          "offset_y": 8
        }
      }
    },
    {
      "order": 3,
      "type": "text",
      "content": "Conquista {STAR} Desbloqueada!",
      "font_url": "https://fonts.gstatic.com/s/roboto/v30/KFOmCnqEu92Fr1Mu4mxP.ttf",
      "font_size": 72,
      "color": "#FFFFFF",
      "x": 60,
      "y": 200,
      "max_width": 680,
      "line_height": 1.3,
      "text_align": "left",
      "opacity": 1.0
    },
    {
      "order": 4,
      "type": "text",
      "content": "Você completou todos os desafios da temporada.",
      "font_url": "https://fonts.gstatic.com/s/roboto/v30/KFOmCnqEu92Fr1Mu4mxP.ttf",
      "font_size": 32,
      "color": "#AAAAFF",
      "x": 60,
      "y": 380,
      "max_width": 680,
      "line_height": 1.5,
      "text_align": "left",
      "opacity": 0.9
    }
  ]
}
```