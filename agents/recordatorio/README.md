# Agente `recordatorio` — pendientes del día

**Qué hace:** cada mañana manda por Telegram la lista de pendientes de configuración y los pasos del
siguiente. Sirve para no olvidar tareas que solo puede hacer la persona (crear cuentas, claves, alertas).

| | |
|---|---|
| Tipo | Lectura de un archivo de notas, sin modelo de lenguaje |
| Frecuencia | Diario a las 9:00 (solo en la Mac) |
| Entradas | Un archivo de notas local, fuera de git (ruta en `PENDIENTES_FILE` de `.env`) |
| Salidas | Un mensaje de Telegram al día como máximo |

## Cómo funciona

- Busca la sección "Pendientes del usuario" y en ella los títulos `### N. [ ] ...` con sus pasos.
- Da por hecho un pendiente cuando su clave ya está en `.env` (ej. "Groq" → `GROQ_API_KEY`), aunque no se
  haya marcado `[x]`.
- Envía la lista y los pasos del primero (hasta 3000 caracteres).

## Cómo funciona por dentro

### En palabras simples

Una vez al día abre un archivo de notas en texto, busca la lista de cosas por hacer y se queda con las que
siguen sin marcar. Si alguna ya está resuelta (porque su clave ya está en `.env`), la salta. Con el resto arma
un mensaje corto: la lista numerada y, debajo, los pasos de la primera, para saber por dónde seguir.

Si ya avisó hoy, no repite. Si Telegram falla, muestra una notificación en la Mac.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones clave |
|---|---|---|
| `agents/recordatorio/run.py` | Lee las notas, filtra lo abierto, arma y envía el mensaje | `pending_items`, `build_message`, `main` |
| `core/config.py` | Carga `.env` y la ruta del proyecto | `env`, `ROOT` |
| `core/db.py` | SQLite compartido; evita repetir el aviso | `connect`, `alert_once` |
| `core/notify.py` | Envío por Telegram (corta en 4000 caracteres) y aviso de macOS | `telegram`, `macos` |

**Una corrida, paso a paso**

1. `main` lee los flags `--ver` y `--forzar`.
2. `pending_items` abre el archivo de notas (`PENDIENTES_FILE`). Si no existe o no tiene la sección, devuelve
   una lista vacía.
3. La regex `SECTION` toma el bloque `## ...Pendientes del usuario...` hasta el siguiente `## `; la regex `ITEM`
   toma cada `### N. [ ] título` con el texto que sigue como pasos. Los `[x]` no coinciden y quedan fuera.
4. Descarta los títulos que contienen una palabra de `DONE_WHEN_SET` cuya variable de `.env` ya tiene valor.
5. Sin pendientes: imprime un aviso y termina sin enviar nada.
6. `build_message` arma la lista numerada y los pasos del primero (sin `` ` `` ni `**`, hasta 3000 caracteres).
7. Con `--ver` solo imprime. Si no, `db.alert_once(conn, "recordatorio:<fecha>")` corta si ya se envió hoy
   (salvo `--forzar`).
8. `notify.telegram(mensaje, "recordatorio")`; si devuelve `False`, `notify.macos` con el total y el primero.

**Estado y datos**

- Lee: un archivo Markdown local, fuera de git, con una sección `## ... Pendientes del usuario ...` y títulos
  `### 1. [ ] Nombre` seguidos de sus pasos.
- Guarda: una fila por día en la tabla `alerts_sent` de `data/agentic.db` (clave `recordatorio:AAAA-MM-DD`).
- Log (con launchd): `data/recordatorio.log`.

**Para replicarlo**

- Dependencias: `python-dotenv` y `requests` (no usa modelo de lenguaje).
- `.env`: `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, u opcionalmente `TELEGRAM_BOT_TOKEN_RECORDATORIO` y
  `TELEGRAM_CHAT_ID_RECORDATORIO` para un bot propio; `PENDIENTES_FILE` con la ruta de las notas.
- Programación: `./scripts/instalar_launchd.sh recordatorio` crea un `StartCalendarInterval` diario a las 9:00.
- Pasos mínimos: crear el archivo de notas con el formato de arriba, completar `.env`, probar con `--ver`,
  luego `--forzar` y por último instalar con launchd.

### Adaptarlo a tu caso

- **Genérico (se reutiliza tal cual):** leer una lista en texto → filtrar lo abierto → mandar un resumen
  diario, con `alert_once` para no repetir y aviso local si falla el envío.
- **Específico (cambiar):** el título de sección en `SECTION`, el formato de ítem en `ITEM`, el mapa
  `DONE_WHEN_SET` (palabra del título → variable de `.env`) y el encabezado del mensaje en `build_message`.
- **Otra lista** (tareas, hábitos, vencimientos): apuntar `PENDIENTES_FILE` al archivo y ajustar las regex.
  Para vencimientos, agregar una fecha en el título y filtrar u ordenar por ella en `pending_items`.
- **Otra hora:** cambiar `HOUR=9` en `scripts/instalar_launchd.sh` (o usar cron en Linux).
- **Otro canal:** reemplazar la llamada a `notify.telegram` por correo, Slack u otro servicio en `main`.

## Decisión de diseño

Las notas son la única fuente: al marcar un pendiente como `[x]` (o al completar su clave en `.env`), deja de
aparecer al día siguiente, sin otra base de datos que mantener.

## Uso

```bash
.venv/bin/python -m agents.recordatorio.run --ver      # solo muestra el mensaje
.venv/bin/python -m agents.recordatorio.run --forzar   # envía aunque ya avisó hoy
```
