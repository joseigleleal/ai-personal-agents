# Agente `freelance` — ofertas freelance con borrador de propuesta

**Qué hace:** busca proyectos freelance, los puntúa según el perfil y, para los mejores, redacta un
borrador de propuesta en el idioma de la oferta. **Nunca envía nada:** la persona revisa y postula.
(Hoy está apagado por decisión personal; el código está completo.)

| | |
|---|---|
| Tipo | Recolección + modelo para puntuar + modelo para redactar |
| Frecuencia | Cada 2 h |
| Entradas | `config/profile.yaml` · `.env` (modelos Ollama/Groq/Claude, IMAP, token de Freelancer.com) |
| Salidas | `data/reports/AAAA-MM-DD.md` · Telegram · SQLite (ofertas, costos) |

## Flujo

```
RemoteOK · Hacker News ("Seeking freelancer", "Who is hiring") · Freelancer.com (API) · alertas por correo
   ──▶ quitar ya vistas ──▶ pre-filtro por palabras ──▶ puntaje 0-10
   ──▶ ≥ umbral_propuesta: borrador (inglés o español) ──▶ reporte + aviso
```

## Cómo funciona por dentro

### En palabras simples

Buscar proyectos freelance es revisar muchas páginas para encontrar pocas ofertas que valgan la pena. El agente
hace esa primera revisión, como un asistente que lee todos los anuncios, descarta los que claramente no sirven,
pone una nota a los demás según tu perfil y, para los mejores, deja escrita una carta de presentación.

Decide solo qué descartar, qué nota poner y en qué idioma escribir. La persona decide todo lo demás: lee el
reporte o el aviso, corrige el borrador y, si le interesa, postula por su cuenta.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones/clases clave |
|---|---|---|
| `run.py` | Orquesta la corrida y sus opciones | `main()` |
| `sources.py` | Trae ofertas de cada fuente y las normaliza | `Offer`, `collect()`, `remoteok()`, `hn_freelancer()`, `freelancer_com()`, `email_alerts()` |
| `scorer.py` | Pre-filtro por palabras, preguntas al modelo, puntaje e idioma | `prefilter()`, `score()`, `compute_score()`, `detect_language()` |
| `writer.py` | Redacta el borrador y limpia marcadores tipo `[Nombre]` | `write_proposal()`, `_clean()` |
| `report.py` | Reporte Markdown del día y avisos | `append_to_report()`, `send_alerts()` |
| `core/llm.py` · `core/db.py` · `core/notify.py` · `core/config.py` | Modelos, SQLite, Telegram/macOS, `.env` y perfil | `backend_chain()`, `claude_json()`, `month_spend()`, `telegram()`, `load_profile()` |

**Una corrida, paso a paso**

1. `load_profile()` lee `config/profile.yaml` (o el `.example`) y `db.connect()` abre la base.
2. `collect()` llama a cada fuente; si una falla, guarda el error y sigue con las demás:
   RemoteOK (`https://remoteok.com/api`), hilos de HN vía Algolia (`hn.algolia.com/api/v1`; en "Who is hiring"
   solo comentarios con CONTRACT/CONTRACTOR/FREELANCE), Freelancer.com (API oficial, solo con token) y correos
   no leídos de Upwork/Workana por IMAP (se extraen los links de oferta; el correo queda marcado como leído).
3. Quita duplicados (id = `fuente:id_externo`) y las ya guardadas (`db.offer_exists`). `--solo-fuentes` termina aquí.
4. `prefilter()`: las que no pasan se guardan como `descartada`, sin gastar modelo.
5. Hasta `--max-puntuar` (40) candidatas: `score()` prueba la cadena de modelos; el modelo responde un JSON con
   preguntas concretas y `compute_score()` calcula el puntaje. Si el modelo falla o no hay ninguno listo, la
   corrida deja de puntuar; las que sobran quedan sin registrar para la próxima.
6. Si el puntaje ≥ `umbral_propuesta`, `write_proposal()` redacta en el idioma detectado (hasta
   `MAX_PROPOSALS_PER_RUN`). Un error del modelo queda como nota en el reporte, no corta la corrida.
7. `append_to_report()` agrega la corrida al reporte con el gasto del mes y `send_alerts()` manda un mensaje por
   oferta a Telegram (o un aviso de macOS si Telegram no está configurado).

**Estado y datos**

- SQLite `data/agentic.db`: tabla `offers` (`id`, `source`, `title`, `url`, `budget`, `score`, `score_reason`,
  `language`, `proposal`, `status`: `descartada` → `puntuada` → `propuesta`) y tabla `costs` (cada llamada a Claude).
- Reporte: `data/reports/AAAA-MM-DD.md`, una sección `## Corrida HH:MM` por ejecución. Los borradores van ahí y en SQLite.

**Reglas y cálculos que deciden**

- Pre-filtro: descarta si hay una palabra de `excluir`; pasa si hay una de `palabras_clave` en título o
  descripción, o una de `palabras_debiles` en el título (palabra completa, minúsculas).
- Puntaje: parte de `skill_match` (0-10) y aplica los topes descritos en "Decisiones de diseño".
- Idioma: español si hay más palabras comunes en español que en inglés; si no, inglés.
- Valores por defecto: `umbral_propuesta` 7 · `MAX_PROPOSALS_PER_RUN` 5 · `MONTHLY_BUDGET_USD` 10 ·
  `SCORER_BACKEND`/`WRITER_BACKEND` `ollama` · `*_FALLBACK` `skip` (sin respaldo).

**Para replicarlo**

- Dependencias: `requests`, `pyyaml`, `python-dotenv`, `anthropic` (`requirements.txt`). Ollama local, o Groq/Claude por API.
- Variables de `.env`: `SCORER_BACKEND`, `SCORER_FALLBACK`, `WRITER_BACKEND`, `WRITER_FALLBACK`, `OLLAMA_URL`,
  `OLLAMA_MODEL`, `OLLAMA_WRITER_MODEL`, `GROQ_API_KEY`, `GROQ_SCORER_MODEL`, `GROQ_WRITER_MODEL`,
  `ANTHROPIC_API_KEY`, `CLAUDE_WRITER_MODEL`, `MONTHLY_BUDGET_USD`, `MAX_PROPOSALS_PER_RUN`, `IMAP_HOST`,
  `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER`, `FREELANCER_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  (opcional un bot propio con sufijo `_FREELANCE`). Todas son opcionales: sin IMAP ni token, esas fuentes se omiten.
- Pasos: copiar `config/profile.example.yaml` a `config/profile.yaml` y completarlo, configurar `.env`,
  probar con `--solo-fuentes`, luego `--sin-propuestas` y, por último, programarlo con `scripts/instalar_launchd.sh freelance`.

### Adaptarlo a tu caso

- **Genérico (se reutiliza tal cual):** fuentes → quitar vistas → pre-filtro → puntuar con modelo + reglas →
  borrador → reporte y aviso. `run.py`, `report.py`, `core/` y el patrón de `compute_score()` no dependen de la plataforma.
- **Otra plataforma:** agregar en `sources.py` una función que devuelva `list[Offer]` y sumarla a `fetchers` en
  `collect()`. Para alertas por correo de otro sitio, agregarlo en `SENDERS` y `JOB_LINK_PATTERNS`.
- **Otro perfil:** solo `config/profile.yaml` (habilidades, logros reales, palabras del pre-filtro, `busquedas`, umbral).
- **Otras reglas de puntaje:** cambiar las preguntas de `SCORER_SYSTEM` y los topes de `compute_score()`.
- **Otro tipo de borrador** (correo, mensaje corto, otro idioma): ajustar `WRITER_SYSTEM` y el `language_name` de
  `write_proposal()`; para más idiomas, extender `detect_language()`.

## Decisiones de diseño (y por qué)

- **Sin scraping de Upwork/Workana:** solo alertas por correo y APIs oficiales.
- **Puntaje con reglas**, igual que `empleo`: rol que no es de desarrollo ≤ 3, presencial ≤ 3, empleo fijo ≤ 5,
  señales de estafa = 0.
- **Idioma detectado en código**, no por el modelo.
- **Costos bajo control:** Ollama (gratis) por defecto; Claude solo opcional, con registro de cada gasto en
  SQLite, tope `MONTHLY_BUDGET_USD` y máximo de propuestas por corrida.

## Uso

```bash
.venv/bin/python -m agents.freelance.run --solo-fuentes
.venv/bin/python -m agents.freelance.run --sin-propuestas   # solo puntúa
.venv/bin/python -m agents.freelance.run
```
