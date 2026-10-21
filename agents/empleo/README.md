# Agente `empleo` — ofertas de trabajo en España

**Qué hace:** junta ofertas de varias fuentes, descarta las que no encajan, las puntúa de 0 a 10 según el
perfil y las reglas personales (jornada, permiso de trabajo, modalidad, ciudad) y avisa las mejores. Si una
oferta excede las horas que permite el permiso de trabajo, propone una nota corta para postular.
**Nunca postula.** Es el **piloto con LangGraph** del repo: ver [docs/langgraph.md](../../docs/langgraph.md).

| | |
|---|---|
| Tipo | Grafo de LangGraph: recolección + modelo para extraer datos + reglas en código + modelo redactor |
| Frecuencia | Cada 2 h (correos en cada corrida; APIs cada `cada_horas_api` h) |
| Entradas | `config/profile.yaml` (habilidades) · `config/empleo.yaml` (preferencias privadas) · claves en `.env` |
| Salidas | Telegram (máx. 10 avisos por corrida) · `data/reports/empleo-AAAA-MM-DD.md` · SQLite · `data/empleo_graph.db` (estado del grafo) |

## Flujo

```
recolectar ─▶ prefiltrar ─▶ puntuar ⟲ (lotes de 10) ─▶ priorizar ─▶ [redactar_notas] ─▶ reportar
```

Cada oferta puntuada queda en un grupo según el permiso de trabajo (`permiso_trabajo.horas_max_semana`):

| Grupo | Cuándo | Se avisa |
|---|---|---|
| ✅ Cumple tu permiso | Jornada parcial, ≤ horas máximas o prácticas | Con puntaje ≥ `umbral_aviso` (+1 punto) |
| 🟢 Por proyecto / freelance | Contrato por proyecto, B2B o autónomo: se pacta la dedicación | ≥ `umbral_aviso`, con nota para postular |
| 🤝 Jornada negociable | Jornada completa, pero dice horario flexible, jornada reducida o semana de 4 días | ≥ `umbral_aviso`, con nota |
| ❔ Jornada sin indicar | El anuncio no lo dice (típico de las alertas por correo) | ≥ `umbral_aviso`, con nota |
| 🟡 Excede tu permiso | Jornada completa sin flexibilidad | Solo si es excelente (`umbral_excede`, máx. `max_avisos_excede`) y −1 punto. El reporte las tiene todas |

**Fuentes (solo canales permitidos):**
- APIs con clave: Adzuna (con una pasada extra solo de jornada parcial, `buscar_parcial`) e InfoJobs.
- APIs públicas sin clave: Get on Board, Remotive, Jobicy (solo remoto abierto a España), bolsa de empleo
  universitaria (RSS) y datos abiertos de empleo público regional. De las APIs públicas se toman solo las
  ofertas de los últimos 14 días (`PUBLIC_MAX_DAYS`).
- Alertas por correo (IMAP, sin marcarlas como leídas): LinkedIn, Indeed, Tecnoempleo, InfoJobs.
  Estos portales prohíben el scraping, así que se crean alertas y el agente lee esos correos.
  Consejo: crea alertas con el filtro de **media jornada** y **prácticas**, no solo las generales.

## Cómo funciona por dentro

### En palabras simples

Buscar trabajo con condiciones especiales (pocas horas por semana, una ciudad, remoto) obliga a revisar a mano
cientos de anuncios que casi nunca encajan. El agente hace esa primera lectura: cada 2 horas junta anuncios de
portales y de tus correos de alerta, tira los que claramente no sirven y lee el resto con un modelo de lenguaje.

Funciona como un asistente con una planilla: el modelo solo llena las casillas (¿es remoto?, ¿cuántas horas?,
¿es freelance?) y una regla fija calcula la nota. Así la nota no depende del "humor" del modelo.

Decide solo qué anuncios mirar, qué nota ponerles y cuáles avisar. Postular, revisar el anuncio completo y usar
(o no) la nota sugerida queda siempre en manos de la persona.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones/clases clave |
|---|---|---|
| `agents/empleo/run.py` | Entrada por consola, checkpointer y reanudación | `main()`, `status()`, `CHECKPOINTS`, `RESUME_MAX_AGE` |
| `agents/empleo/graph.py` | Grafo de LangGraph y sus nodos | `EmpleoState`, `build()`, `WRITER_SYSTEM`, `BATCH` |
| `agents/empleo/sources.py` | Fuentes (APIs, RSS, IMAP) y turnos por fuente | `collect()`, `adzuna()`, `email_alerts()`, `_api_turn()` |
| `agents/empleo/scorer.py` | Pre-filtro, prompt, reglas de puntaje y grupo del permiso | `prefilter()`, `score()`, `compute_score()`, `permit_fit()`, `PERMITS` |
| `agents/empleo/report.py` | Reporte Markdown del día y avisos | `write_report()`, `send_alerts()` |
| `core/llm_lc.py` | Modelos LangChain con respaldo y registro de costo | `chat_model()`, `json_chain()`, `text_chain()`, `ClaudeCost` |
| `core/db.py`, `core/notify.py` | SQLite compartida y Telegram (respaldo: aviso de macOS) | `insert_offer()`, `alert_once()`, `telegram()` |

La clase `Offer` (fuente, id externo, título, url, descripción, salario, `applied`) se reutiliza de `agents/freelance/sources.py`.

**Una corrida, paso a paso**

1. `run.py` compila el grafo con `SqliteSaver` (`data/empleo_graph.db`, `thread_id="empleo"`). Si hay una
   corrida a medias de menos de 24 h, la retoma desde el nodo pendiente; si no, empieza otra y arrastra las
   `pendientes` de la anterior. El estado es el `TypedDict` `EmpleoState` (`nuevas`, `pendientes`, `elegidas`,
   `avisos`, `errores`, `sin_modelo`...), con las ofertas como dict para que se puedan guardar.
2. `recolectar`: `sources.collect()` llama a cada fuente en un `try` (una caída queda en `errores` y no frena a
   las demás). Correos IMAP en cada corrida; APIs una vez por bloque de `cada_horas_api` h (marca en
   `alerts_sent`). APIs: `api.adzuna.com/v1/api/jobs/{pais}/search/1`, `api.infojobs.net/api/9/offer`,
   `getonbrd.com/api/v0/search/jobs`, `remotive.com/api/remote-jobs`, `jobicy.com/api/v2/remote-jobs`, más un
   RSS universitario y un dataset de datos abiertos regional. Las ya postuladas (correos "se ha enviado tu
   solicitud") se guardan como `postulada`; solo siguen las que no están en la base.
3. `prefiltrar`: `scorer.prefilter()` (palabras de `excluir` y `palabras_clave`, país en el título) sin modelo.
   Las que no pasan se guardan como `descartada`.
4. `puntuar` (bucle): lotes de `BATCH = 10`, hasta `--max-puntuar` (60 por defecto). Cada oferta va a
   `json_chain(SCORER_SYSTEM, modelo)`: prompt → modelo en modo JSON, temperatura 0 → dict. Si fallan el modelo
   principal y su respaldo, se corta el bucle, lo que falta queda en `pendientes` y el error va al reporte.
5. `priorizar`: ordena por grupo del permiso y puntaje, aplica `umbral_excede`/`max_avisos_excede` y el tope
   `max_avisos_por_corrida`.
6. `redactar_notas` (solo si algún aviso no es ✅): `text_chain(WRITER_SYSTEM)` con temperatura 0,5 escribe una
   nota de hasta 3 frases. Si falla, el aviso sale sin nota.
7. `reportar`: agrega la corrida a `data/reports/empleo-AAAA-MM-DD.md` y envía los avisos por Telegram.

**Estado y datos**

| Dónde | Qué guarda |
|---|---|
| `data/agentic.db` → `offers` | `id` (`fuente:id_externo`), `source`, `title`, `url`, `description`, `budget`, `seen_at`, `score`, `score_reason`, `status` (`descartada`, `puntuada`, `postulada`) |
| `data/agentic.db` → `alerts_sent` | Turnos de cada API (`fuente:<nombre>:<fecha>:<bloque>`) |
| `data/agentic.db` → `costs` | Gasto de Claude si se usa (`ClaudeCost`, propósitos `puntuar_empleo` y `nota_empleo`) |
| `data/empleo_graph.db` | Checkpoints del grafo (se borran al empezar una corrida nueva) |
| `data/reports/empleo-AAAA-MM-DD.md` | Una sección por corrida, agrupada por `PERMITS` |

**Reglas y cálculos que deciden**

- El modelo solo extrae (ver `SCORER_SYSTEM`): `skill_match` 0-10, `role_is_technical`, `schedule`,
  `weekly_hours`, `hours_flexible`, `contract_type`, `work_mode`, `city`, `remote_only_other_country`,
  `is_internship`, `requires_unrestricted_permit`, `red_flags` y una razón corta.
- El código decide: `compute_score()` parte de `skill_match` y aplica topes y bonos; `permit_fit()` asigna el
  grupo. Las alertas por correo (marca `EMAIL_MARK`) ignoran lo que el modelo diga de jornada o permiso.
- Claves y valores por defecto del código: `umbral_aviso` 7, `umbral_excede` 9, `max_avisos_excede` 2,
  `max_avisos_por_corrida` 10, `cada_horas_api` 6, `priorizar_permiso` true, `remoto` true,
  `jornadas` [parcial, completa], `pais` es. Sin `horas_max_semana`, todo cae en ✅ (la nota usa 30 h).
- El porqué de cada regla está en [Decisiones de diseño](#decisiones-de-diseño-y-por-qué).

**Para replicarlo**

- `pip install -r requirements.txt` (para este agente: `langgraph`, `langgraph-checkpoint-sqlite`,
  `langchain-core`, `langchain-ollama`, `langchain-groq`, `langchain-anthropic`, `requests`, `certifi`, `pyyaml`).
- Modelo: Ollama local, Groq (plan gratis) o Claude; basta uno. Telegram es opcional.
- `.env` por nombre: `EMPLEO_SCORER_BACKEND`, `EMPLEO_SCORER_FALLBACK`, `EMPLEO_OLLAMA_MODEL` (o los generales
  `SCORER_BACKEND`, `SCORER_FALLBACK`), `WRITER_BACKEND`, `WRITER_FALLBACK`, `OLLAMA_URL`, `OLLAMA_MODEL`,
  `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_SCORER_MODEL`, `ANTHROPIC_API_KEY`, `MONTHLY_BUDGET_USD`,
  `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `INFOJOBS_CLIENT_ID`, `INFOJOBS_CLIENT_SECRET`, `IMAP_HOST`, `IMAP_USER`,
  `IMAP_PASSWORD`, `IMAP_FOLDER`, `TELEGRAM_BOT_TOKEN[_EMPLEO]`, `TELEGRAM_CHAT_ID[_EMPLEO]`.
  Las fuentes sin clave se saltan solas.
- Pasos: copiar `config/profile.example.yaml` y `config/empleo.example.yaml`, completar `.env`, correr
  `--solo-fuentes` y luego la corrida real (ver [Uso](#uso)).

### Adaptarlo a tu caso

- **Genérico (se reutiliza tal cual):** el patrón juntar ítems de varias fuentes → pre-filtro barato → el modelo
  extrae datos en JSON → reglas en código puntúan → avisar los mejores; el grafo con checkpointer reanudable;
  `core/llm_lc.py` (modelo con respaldo y tope de gasto), `core/db.py`, `core/notify.py` y `report.py` casi igual.
- **Específico:** fuentes de España, el idioma del prompt, el permiso de trabajo con límite de horas y la nota
  para postular.
- **Otro país:** cambiar `pais` (Adzuna acepta otros códigos), `ciudades_presencial` y `busquedas`; quitar o
  reemplazar en `collect()` las fuentes locales (InfoJobs, RSS universitario, dataset regional) y ajustar
  `REMOTE_FROM_SPAIN`, `OPEN_TO_SPAIN` y la primera línea de `SCORER_SYSTEM`.
- **Otra búsqueda (pisos, becas, productos):** escribir funciones en `sources.py` que devuelvan `Offer`; cambiar
  las preguntas de `SCORER_SYSTEM` por los datos que importan (precio, metros, plazo...); reescribir
  `compute_score()` y reemplazar `PERMITS`/`permit_fit()` por tus propios grupos; ajustar textos de `report.py`.
- **Otras reglas:** si no tienes límite de horas, deja vacío `horas_max_semana`; los umbrales y topes se cambian
  en `config/empleo.yaml` sin tocar código. Si no necesitas nota, borra el nodo `redactar_notas` en `build()`.

## Decisiones de diseño (y por qué)

- **El modelo extrae, el código decide.** Se le hacen preguntas concretas (¿remoto?, ¿horas?, ¿contrato por
  proyecto?, ¿horario flexible?) y el puntaje se calcula con reglas. Así un modelo chico y gratis no infla
  resultados. Lo que se puede saber por el título (país, puesto de dirección) lo decide una expresión regular.
- **El permiso de trabajo ordena todo.** Con muchas ofertas remotas de jornada completa, avisar todo lo que
  "encaja" llenaba Telegram de ofertas que luego descartaban por las horas. Ahora se avisa primero lo que se
  puede aceptar tal cual y lo que se puede negociar, con una nota que pone las horas por delante.
- **LangGraph con checkpointer.** El estado se guarda tras cada paso. Si la corrida se corta (sin red, Groq
  caído, Mac dormida), la siguiente la retoma; las ofertas que no alcanzaron a puntuarse quedan pendientes.
- **Topes duros:** fuera de la ciudad y no remoto → máx. 2; remoto solo para residentes de otro país → se
  descarta si el título lo dice, máx. 2 si lo detecta el modelo en el anuncio; dirección de equipos → máx. 4; pide nacionalidad UE → máx. 3; prácticas +1.
- **Ofertas con pocos datos** (solo título y ciudad, típico de las alertas): tope de 8 y "revísalo en el anuncio".
- **Modelo propio por agente:** `EMPLEO_SCORER_BACKEND=groq` con respaldo `ollama` (`with_fallbacks`),
  temperatura 0 para que el mismo anuncio dé el mismo resultado.
  Con `EMPLEO_OLLAMA_MODEL` el respaldo local puede ser un modelo chico afinado para esta tarea
  (ver [experimento LoRA con MLX](../../experimentos/lora_mlx/README.md)).

## Uso

```bash
cp config/empleo.example.yaml config/empleo.yaml
.venv/bin/python -m agents.empleo.run --solo-fuentes   # prueba fuentes, no gasta turnos ni avisa
.venv/bin/python -m agents.empleo.run                  # corrida real
.venv/bin/python -m agents.empleo.run --estado         # ¿quedó una corrida a medias?
.venv/bin/python -m unittest tests.test_empleo -v      # pruebas sin red ni claves
./scripts/instalar_launchd.sh empleo
```

## Límites

- Jornada y horas salen del texto del anuncio: si no las dice, queda en ❔ y hay que mirarlo.
- La nota para postular es un borrador: revísala antes de usarla.
- Confirma las condiciones de tu permiso de trabajo con la fuente oficial; el agente solo aplica el número de
  horas que le das.
