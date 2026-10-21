# Personal Agents

Agentes autónomos de uso personal: trabajan en segundo plano y avisan por Telegram cuando hay algo que
revisar. El repo está pensado como **ejemplo para replicar**: cada agente es pequeño, se entiende en una
lectura y comparte la misma base.

## Agentes

| Agente | Qué hace | Modelo de lenguaje | Frecuencia | Ficha |
|---|---|---|---|---|
| `cita` | Busca y reserva cita previa de extranjería (toma de huellas) entrando con certificado digital; reintenta los pasos que fallan, aprende a qué horas aparecen citas y qué tan seguido puede consultar, y responde comandos por Telegram (estado, informes, capturas) | No (navegador con Playwright) | Aprendida: 1–30 min | [README](agents/cita/README.md) |
| `empleo` | Junta ofertas de empleo en España, las puntúa con reglas personales (incluido el límite de horas del permiso de trabajo) y avisa las mejores. **Piloto con LangGraph** | Sí, para extraer datos y redactar notas | 2 h | [README](agents/empleo/README.md) |
| `freelance` | Busca proyectos freelance y redacta borradores de propuesta | Sí, para puntuar y redactar | 2 h | [README](agents/freelance/README.md) |
| `fx` | Avisa cuando la tasa EUR → COP está barata frente a los últimos 90 días | No | 4 h | [README](agents/fx/README.md) |
| `recordatorio` | Envía cada mañana los pendientes de configuración y sus pasos | No | Diario 9:00 | [README](agents/recordatorio/README.md) |

¿Quieres hacer uno propio? → [Cómo crear un agente nuevo](docs/crear-un-agente.md) · [Agentes con LangChain y LangGraph](docs/langgraph.md)

## Cómo funciona

Cada agente es un programa de Python que **hace una corrida y termina**. El sistema operativo lo repite cada
cierto tiempo. En cada corrida hace su tarea, guarda el estado en SQLite y avisa por Telegram. Nunca envía ni
paga nada por su cuenta: propone y la persona decide (salvo que la persona pida lo contrario, como la reserva
de `cita`).

```
programador (launchd en Mac / systemd en servidor)
   └─▶ servidor: scripts/run_agent.sh       git pull + pip install, y luego lo mismo que la Mac
   └─▶ Mac: python -m agents.<agente>.run   directo, con el código tal como está en la carpeta
          └─▶ agents/<agente>/run.py        la lógica propia del agente
                 └─▶ core/                  config · SQLite · modelos de lenguaje · avisos
                        └─▶ Telegram · data/reports/ · data/agentic.db · data/<agente>.log
```

| Pieza | Qué se usa |
|---|---|
| Lenguaje | Python 3.12 con entorno virtual en `.venv/` |
| Datos | SQLite (`data/agentic.db`): ofertas, tasas, avisos enviados, costos |
| Modelos | A elección en `.env`: **Ollama** (local, gratis), **Groq** (API, plan gratis) o **Claude** (de pago, con tope mensual) |
| Orquestación | Python simple en casi todos; **LangGraph + LangChain** (gratis, código abierto) en `empleo`, con estado que se retoma |
| Navegador | Playwright (Chromium) para sitios sin API |
| Avisos | Bot de Telegram (uno general o uno por agente) y notificación de macOS de respaldo |
| Programación | `launchd` en la Mac · timers de `systemd` en el servidor |
| Servidor | Oracle Cloud Always Free (Ubuntu 24.04) |
| Configuración | `.env` (secretos) y `config/*.yaml` (preferencias), solo locales y fuera de git |

## Cómo funciona por dentro

### En palabras simples

El trabajo se reparte en tres partes. El **programador del sistema** (el reloj de la Mac o del servidor) decide
*cuándo* corre cada agente. Cada **agente** sabe *qué* hacer: dónde buscar, qué vale la pena y qué decir. La
**base común** (`core/`) resuelve lo que todos necesitan igual: leer la configuración, guardar lo que ya vio,
hablar con un modelo de lenguaje y avisar.

Cada agente corre una vez y termina, como una alarma que suena, hace su tarea y se apaga. Si una corrida falla,
la siguiente empieza limpia; no hay un programa encendido todo el día que se pueda quedar colgado. Lo que debe
recordar entre corridas (ofertas vistas, avisos ya enviados, gastos) queda en un archivo de base de datos.

Avisa por Telegram y, si no puede, con una notificación de la Mac. Lo privado (claves, datos personales,
resultados) vive solo en la máquina, en archivos que git ignora, y un control antes de cada commit frena lo que
parezca un secreto.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones clave |
|---|---|---|
| `core/config.py` | Carga `.env` al importarse y define rutas (`ROOT`, `DATA_DIR`, `REPORTS_DIR`, `DB_PATH`) | `env`, `env_int`, `env_float`, `load_config` (usa `<nombre>.yaml` o, si falta, `<nombre>.example.yaml`), `load_profile`, `profile_summary` |
| `core/db.py` | SQLite en `data/agentic.db`; crea las tablas `offers`, `costs`, `fx_quotes`, `alerts_sent`, `income` si no existen | `connect`, `insert_offer`, `update_offer`, `alert_once`, `record_cost`, `month_spend` |
| `core/llm.py` | Clientes de Ollama (HTTP local), Groq (API compatible con OpenAI) y Claude (SDK de Anthropic) | `ollama_json/text`, `groq_json/text`, `claude_json/text`, `backend_chain`, `is_ready` |
| `core/llm_lc.py` | Los mismos backends como modelos de LangChain, con respaldo y costo por callback | `chat_model`, `json_chain`, `text_chain`, `ClaudeCost` |
| `core/notify.py` | Telegram (texto, foto, álbum) con un reintento y notificación de macOS | `telegram`, `telegram_foto`, `telegram_album`, `macos` |
| `scripts/run_agent.sh` | Servidor: `git pull --ff-only`, `pip install -r requirements.txt` y corre el agente | — |
| `scripts/instalar_launchd.sh` | Mac: escribe `~/Library/LaunchAgents/com.agentic.<agente>.plist` con su intervalo o hora y lo carga | `[agente] desinstalar` para quitarlo |
| `scripts/setup_oracle.sh` | Servidor Ubuntu: paquetes, Ollama si hay 8 GB de RAM o más (si no, swap y Groq), `.venv` y un `.service` + `.timer` de systemd por agente | `install_agent <agente> <intervalo>` |
| `scripts/estado.sh`, `detener.sh`, `reanudar.sh` | Ver, pausar y volver a cargar los agentes de la lista `AGENTS` y Ollama | — |
| `scripts/hooks/pre-commit` | Cancela el commit si hay archivos privados o patrones de secretos | Patrones extra en `.secret-patterns` |

**Una corrida, paso a paso**

1. launchd (por `StartInterval` o `StartCalendarInterval`) ejecuta `.venv/bin/python -m agents.<agente>.run`
   en la carpeta del repo; en el servidor, el timer `agentic-<agente>.timer` lanza el servicio, que llama a
   `scripts/run_agent.sh <agente>` (actualiza código y dependencias antes).
2. Al importar `core.config` se carga `.env`; el agente lee sus preferencias con `load_config("<agente>")`.
3. `db.connect()` abre `data/agentic.db` y asegura el esquema. El agente consulta qué ya vio (`offer_exists`).
4. Trabaja: trae datos (API, RSS, correo o navegador), pide al modelo datos concretos y calcula el resultado con
   reglas en código.
5. Para no repetir avisos usa `db.alert_once(conn, "<clave>")`: devuelve `True` solo la primera vez.
6. Avisa con `notify.telegram(texto, "<agente>")`: usa `TELEGRAM_BOT_TOKEN_<AGENTE>` y `TELEGRAM_CHAT_ID_<AGENTE>`
   si existen, si no los generales; corta en trozos de 4000 caracteres y deja el motivo de un fallo en
   `notify.ultimo_error` (sin el token). Si devuelve `False`, el agente llama a `notify.macos(...)`.
7. Lo que imprime va a `data/<agente>.log` en la Mac (`StandardOutPath`) o a `journalctl -u agentic-<agente>`
   en el servidor; los reportes largos, a `data/reports/`. El proceso termina.

**Modelos de lenguaje**

- La elección va en `.env`: `SCORER_BACKEND` / `SCORER_FALLBACK` y `WRITER_BACKEND` / `WRITER_FALLBACK`
  (o los del agente, como `EMPLEO_SCORER_BACKEND`). Valores: `ollama`, `groq` o `claude`.
- `llm.backend_chain(...)` arma la lista (principal y respaldo, sin repetir) y el agente prueba en orden los que
  `llm.is_ready(...)` da por listos (Ollama responde en `OLLAMA_URL`, hay `GROQ_API_KEY` o `ANTHROPIC_API_KEY`).
  Si uno lanza `LLMError`, sigue con el siguiente. En `llm_lc.chat_model` el respaldo es `.with_fallbacks()`.
- Modelos: `OLLAMA_MODEL`, `GROQ_MODEL` (y variantes por tarea como `GROQ_SCORER_MODEL`), `CLAUDE_WRITER_MODEL`.
  Groq espera y reintenta ante el límite por minuto del plan gratis.
- Tope de gasto: antes de cada llamada a Claude se compara `db.month_spend` con `MONTHLY_BUDGET_USD` (por defecto
  10); si se alcanzó, lanza `BudgetExceeded` (en `llm_lc`, Claude sale de la cadena). Cada llamada guarda tokens
  y USD en la tabla `costs` según la tabla `PRICES`. Ollama y Groq no se registran porque no cobran.

**Para replicarlo**

1. Python 3.12: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
2. `cp .env.example .env` y completa lo que uses; `cp config/<nombre>.example.yaml config/<nombre>.yaml`.
3. Mac: `./scripts/instalar_launchd.sh <agente>`. Servidor Ubuntu: `./scripts/setup_oracle.sh` (crea los timers de
   `freelance`, `fx` y `empleo`; para otro, agrega `install_agent <agente> <intervalo>`).
4. `git config core.hooksPath scripts/hooks` para activar el pre-commit.

### Adaptarlo a tu caso

- **Reutilizable tal cual:** `core/`, `scripts/run_agent.sh`, el hook y el molde `agents/<nombre>/run.py` que
  corre una vez y termina.
- **Otro canal de aviso** (Slack, correo, Discord): agrega una función en `core/notify.py` con la misma firma que
  `telegram(texto, agente)` que devuelva `True`/`False`, y cambia la llamada en el `run.py` o `report.py` del agente.
- **Otro programador:** cualquier cosa que ejecute `.venv/bin/python -m agents.<agente>.run` desde la carpeta del
  repo sirve: una línea de `cron` (`0 */2 * * * cd <repo> && .venv/bin/python -m agents.fx.run >> data/fx.log 2>&1`)
  o una tarea del Programador de tareas de Windows con `.venv\Scripts\python.exe` y esa carpeta como inicio.
- **Otra base de datos:** todo pasa por `core/db.py`; reemplaza `connect` y las funciones de ayuda (el SQL es
  estándar salvo `INSERT OR IGNORE`).
- **Otro proveedor de modelos:** agrega `<proveedor>_json/text` en `core/llm.py`, súmalo a `BACKENDS` e `is_ready`,
  y en `core/llm_lc.py` devuelve su `ChatModel` en `_model`. Si cobra, registra el costo con `db.record_cost`.
- **Un agente nuevo:** sigue [Cómo crear un agente nuevo](docs/crear-un-agente.md).

## Estructura

```
core/            compartido: config.py · db.py · llm.py (modelos) · llm_lc.py (modelos vía LangChain) · notify.py
agents/<nombre>/ un paquete por agente: README.md (su ficha), run.py y, si hace falta, sources.py / scorer.py
config/          *.example.yaml públicos → copiarlos a *.yaml (privados) y completarlos
docs/            guías: crear un agente y agentes con LangGraph
scripts/         launchd, estado, detener/reanudar, servidor Oracle, hook de pre-commit
tests/           pruebas sin red ni claves (.venv/bin/python -m unittest discover tests)
data/            base de datos, reportes, logs y certificados (nunca se sube)
```

## Puesta en marcha

```bash
git clone https://github.com/<usuario>/ai-personal-agents.git && cd ai-personal-agents
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # solo si usarás el agente cita
cp .env.example .env                                 # Telegram, modelos y claves
cp config/profile.example.yaml config/profile.yaml  # y los config/<agente>.example.yaml que uses
git config core.hooksPath scripts/hooks              # revisión de secretos antes de cada commit

.venv/bin/python -m agents.fx.run --resumen          # prueba rápida: debería llegar un Telegram
./scripts/instalar_launchd.sh fx                     # programarlo en la Mac
```

**Configurar Telegram** (todos los agentes avisan por ahí):
1. En Telegram, habla con **@BotFather** → `/newbot` → nombre y usuario (debe terminar en `bot`) → copia el token
   a `TELEGRAM_BOT_TOKEN` en `.env`.
2. Abre el chat de tu bot y envíale `/start`: un bot no puede escribirte hasta que tú le escribas primero.
3. Tu `chat_id`: escríbele a **@userinfobot** (responde con tu id) → `TELEGRAM_CHAT_ID`. En chats privados es el
   mismo para todos tus bots.
4. Opcional, un bot por agente (separa los avisos y permite silenciar uno): crea otro bot, envíale `/start` y pon
   `TELEGRAM_BOT_TOKEN_<AGENTE>` (ej. `TELEGRAM_BOT_TOKEN_CITA`). Si falta, el agente usa el general.
5. Prueba: `.venv/bin/python -c "from core import notify; print(notify.telegram('hola', 'fx'))"` → `True`.

**Elegir el modelo:** en `.env`, `SCORER_BACKEND` (puntuar) y `WRITER_BACKEND` (redactar) aceptan `ollama`,
`groq` o `claude`; `*_FALLBACK` define un respaldo. Con Ollama: `ollama pull qwen3:8b` (~5 GB de RAM).

**Día a día en la Mac**

| Comando | Para qué |
|---|---|
| `./scripts/estado.sh` | Qué está programado, última corrida, configuración pendiente y git |
| `./scripts/instalar_launchd.sh <agente> [desinstalar]` | Programar o quitar un agente |
| `./scripts/detener.sh` / `./scripts/reanudar.sh` | Pausar todo (agentes y Ollama) y volver a encenderlo |
| `tail -f data/<agente>.log` | Ver un agente en vivo |

## Servidor en Oracle Cloud (Always Free)

```bash
# Desde la Mac: reintenta crear las VMs hasta que Oracle tenga capacidad (un pedido cada 3 min)
nohup caffeinate -i ./scripts/oracle_crear_vm.sh >/dev/null 2>&1 &   # micro (1 GB) y a1 (ARM)

# En el servidor. Con menos de 8 GB de RAM no instala Ollama y deja los modelos en Groq
git clone https://github.com/<usuario>/ai-personal-agents.git ~/personal-agents
cd ~/personal-agents && ./scripts/setup_oracle.sh   # instala todo y activa los timers de freelance, fx y empleo (cita: oracle_cita.sh)

# Desde la Mac: el agente cita en la VM A1, con claves cifradas (ver agents/cita/README.md)
./scripts/oracle_cita.sh ubuntu@<ip>
```

Para actualizar el servidor basta con `git push`: en cada corrida hace `git pull`.

## Repo público

Se trabaja directo en este repo, así que lo personal vive solo en la máquina: `.env`, `config/*.yaml`
(salvo los `.example`) y `data/` están en `.gitignore`. El hook `scripts/hooks/pre-commit` cancela el commit
si encuentra archivos privados o algo que parezca un secreto (claves de API, tokens, llaves privadas,
correos). Para frenar también nombres propios, agrégalos uno por línea en `.secret-patterns` (local).
