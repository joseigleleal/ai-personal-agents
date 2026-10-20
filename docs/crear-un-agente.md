# Cómo crear un agente nuevo

Todos los agentes siguen el mismo molde. Si respetas estas piezas, el agente se programa, se despliega y
avisa igual que los demás sin tocar nada más.

## 1. El molde

```
agents/<nombre>/
  README.md       # ficha del agente (GitHub la muestra al abrir la carpeta)
  __init__.py
  run.py          # punto de entrada: python -m agents.<nombre>.run
  sources.py      # (opcional) de dónde saca los datos
  scorer.py       # (opcional) cómo decide qué vale la pena
config/<nombre>.example.yaml   # preferencias de ejemplo (público)
config/<nombre>.yaml           # preferencias reales (privado, en .gitignore)
```

`run.py` hace **una sola corrida y termina**. Quien lo repite es el programador del sistema
(launchd o systemd), no un `while True`. Así, si falla, la próxima corrida empieza limpia.

**Dos estilos:** `run.py` simple, con los pasos en orden (como `fx` o `freelance`), o un grafo de
**LangGraph** con estado que se retoma si la corrida se corta (como `empleo`). Usa el simple si el flujo es
lineal; LangGraph si tiene ramas, ciclos, pasos largos o pausas para aprobar. Guía:
[agentes con LangChain y LangGraph](langgraph.md).

## 2. Las piezas compartidas (`core/`)

| Necesitas | Usa |
|---|---|
| Secretos y rutas | `core.config.env("CLAVE")`, `DATA_DIR`, `load_config("<nombre>")` |
| Guardar estado / no repetir avisos | `core.db.connect()` (SQLite en `data/agentic.db`) |
| Un modelo de lenguaje | `core.llm` → Ollama, Groq o Claude según `.env`, con respaldo y tope de gasto |
| Un modelo vía LangChain | `core.llm_lc.chat_model(...)` + `json_chain(...)`: mismos backends y tope, con `.with_fallbacks()` |
| Avisar | `core.notify.telegram(texto, "<nombre>")` (bot propio si existe `TELEGRAM_BOT_TOKEN_<NOMBRE>`); imágenes con `telegram_foto` (admite botones) o `telegram_album` (hasta 10) |

## 3. Reglas que siguen todos

1. **Proponer, no actuar.** Nada de enviar, pagar o publicar en tu nombre sin aprobación. La excepción
   tiene que pedirla la persona de forma explícita (como la reserva automática del agente `cita`).
2. **Canales permitidos.** APIs oficiales, RSS, datos abiertos y alertas por correo. Si un sitio prohíbe
   bots o scraping, no se fuerza.
3. **El modelo extrae, el código decide.** Pregúntale datos concretos y calcula el resultado con reglas.
   Sale más barato, se puede explicar y no depende de que el modelo sea grande.
4. **Gratis primero.** Ollama o Groq por defecto; los servicios pagos, opcionales y con tope.
5. **Nada privado en git.** Datos personales en `.env`, `config/<nombre>.yaml` o `data/`. El hook
   `scripts/hooks/pre-commit` frena secretos y nombres listados en `.secret-patterns`.
6. **Diagnóstico fácil.** Imprime una línea por corrida (va al log) y guarda lo necesario para entender
   un fallo (reportes, capturas).

## 4. Programarlo

1. Agrega el agente con su intervalo en `scripts/instalar_launchd.sh` (Mac) y, si corre en el servidor,
   en `scripts/setup_oracle.sh` (systemd).
2. Agrégalo a la lista `AGENTS` de `scripts/estado.sh`, `scripts/detener.sh` y `scripts/reanudar.sh`.
3. `./scripts/instalar_launchd.sh <nombre>` y revisa `data/<nombre>.log`.
4. Si usa LangGraph, agrega una opción `--estado` y muéstrala en `estado.sh` (como `empleo`).

## 5. Documentarlo

Crea `agents/<nombre>/README.md` con la misma estructura que las otras fichas (qué hace, tabla de tipo,
frecuencia, entradas y salidas, flujo, decisiones y uso) y agrega una fila en la tabla del `README.md`.
Si tiene reglas que deciden (puntajes, filtros), agrega pruebas en `tests/test_<nombre>.py` con `unittest`;
para el modelo, `FakeListChatModel` de LangChain evita usar red y claves.
