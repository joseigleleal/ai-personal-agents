# Personal Agents

Agentes autónomos de uso personal: trabajan en segundo plano y avisan por Telegram cuando hay algo que
revisar. El repo está pensado como **ejemplo para replicar**: cada agente es pequeño, se entiende en una
lectura y comparte la misma base.

## Agentes

| Agente | Qué hace | Modelo de lenguaje | Frecuencia | Ficha |
|---|---|---|---|---|
| `cita` | Busca y reserva cita previa de extranjería (toma de huellas) entrando con certificado digital; reintenta los pasos que fallan y aprende a qué horas aparecen citas y qué tan seguido puede consultar | No (navegador con Playwright) | Aprendida: 1–30 min | [README](agents/cita/README.md) |
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
cd ~/personal-agents && ./scripts/setup_oracle.sh   # instala todo y activa un timer por agente
```

Para actualizar el servidor basta con `git push`: en cada corrida hace `git pull`.

## Repo público

Se trabaja directo en este repo, así que lo personal vive solo en la máquina: `.env`, `config/*.yaml`
(salvo los `.example`) y `data/` están en `.gitignore`. El hook `scripts/hooks/pre-commit` cancela el commit
si encuentra archivos privados o algo que parezca un secreto (claves de API, tokens, llaves privadas,
correos). Para frenar también nombres propios, agrégalos uno por línea en `.secret-patterns` (local).
