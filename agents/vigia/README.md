# Agente `vigia` — audita a los demás agentes

**Qué hace:** revisa desde afuera que los otros agentes estén funcionando (que revisen a tiempo, que no fallen
seguido, que no queden en pausa eterna) y que el servidor tenga disco y memoria. Solo avisa cuando algo anda mal,
una vez por problema, y manda un parte diario en silencio.

| | |
|---|---|
| Tipo | Reglas sobre archivos de estado, logs y systemd, sin modelo de lenguaje |
| Frecuencia | Cada 2 min (timer de systemd en el servidor), para atender rápido los botones de los picos |
| Entradas | `config/vigia.yaml` · memoria y logs de cada agente en `data/` · `systemctl` · disco y RAM |
| Salidas | Telegram (problema nuevo con sonido; tablero que se edita en cada vuelta, "resuelto" y parte diario en silencio) · `data/vigia.json` · `data/vigia.log` |

## Flujo

```
cada 30 min ──▶ por agente: ¿timer encendido? (apagado = detenido a propósito, no es problema)
                  ├─ cita: ¿revisó a la hora prevista? ¿% de errores en la hora? ¿pausa demasiado larga?
                  └─ por corridas (empleo): ¿el log creció hace poco? ¿líneas de error en la última corrida?
             ──▶ servidor: % de CPU (muestra de 2 s), carga 1/5/15 min, RAM y disco en GB, procesos que más consumen,
                 servicios necesarios (ej. ollama)
             ──▶ tablero: edita el mismo mensaje de Telegram con todo lo anterior (sin sonido)
             ──▶ ¿pico? (CPU ≥ 90 % y carga de 1 min alta, o poca RAM) → 🔥 qué lo genera + botones de la lista
                 cerrada (descargar Ollama, detener un proceso, pausar cita); el botón se ejecuta en la vuelta siguiente
             ──▶ problemas nuevos → ⚠️ con sonido · los que desaparecieron → ✅ en silencio
             ──▶ una vez al día (parte_diario_hora): 🛰️ parte de todos, en silencio
```

## Cómo funciona por dentro

### En palabras simples

Es un guardia que da una vuelta cada dos minutos. No sabe hacer el trabajo de los otros agentes ni los toca: solo
mira las huellas que dejan (a qué hora dijeron que volverían a revisar, cuántas veces fallaron, si su servicio se
cayó) y el estado del servidor. Si algo no cuadra, avisa una sola vez; cuando se arregla, avisa sin hacer ruido.
Así uno no tiene que preguntar "¿cómo va?" ni leer logs para enterarse de que algo se detuvo.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones clave |
|---|---|---|
| `agents/vigia/run.py` | Corre los chequeos, avisa cambios y arma el parte | `auditar`, `revisar_servicio`, `revisar_cita`, `revisar_log`, `revisar_sistema`, `avisar_cambios`, `texto_parte` |
| `config/vigia.example.yaml` | Agentes a vigilar, umbrales y hora del parte | `agentes`, `sistema`, `parte_diario_hora` |
| `scripts/oracle_vigia.sh` | Lo instala en la VM: bot cifrado con systemd-creds, servicio y timer | — |
| `tests/test_vigia.py` | Reglas y "avisar una vez" sin red ni systemd | — |

**Una corrida, paso a paso**

1. `auditar` recorre `agentes:` de la config. Con `systemctl is-enabled <servicio>.timer` decide si el agente está
   activo; si el timer está apagado lo anota como detenido y no lo juzga. `is-failed` detecta un servicio caído.
2. `revisar_cita` lee `data/cita/aprendizaje.json`: si pasó `atraso_max_min` desde `proxima` (y no hay pausa
   vigente en `pausa_hasta`), el agente está quieto; calcula el % de revisiones con error o bloqueo en la última
   hora y el ciclo real (mediana entre inicios); una pausa mayor a `pausa_max_min` también es problema.
3. `revisar_log` (agentes por corridas): antigüedad del log frente a `log_max_min` y cuántas de las últimas 40 líneas
   contienen alguna de `frases_error`.
4. `revisar_sistema`: `uso_cpu` (dos lecturas de `/proc/stat` separadas 2 s), `os.getloadavg` (1/5/15 min),
   `MemAvailable` de `/proc/meminfo`, `shutil.disk_usage`, `procesos_top` (`ps --sort=-pcpu`) y
   `systemctl is-active` de cada servicio. Solo la carga de 15 min por núcleo (`carga_max_por_nucleo`, 1,5) cuenta
   como problema: un pico mientras un modelo responde es normal.
5. `avisar_cambios` compara con `data/vigia.json` (`abiertos`: clave → desde cuándo): lo nuevo se avisa con sonido;
   lo que ya no aparece se avisa "resuelto" en silencio. Solo se guarda si Telegram lo entregó.
6. Con `tablero: true`, edita el mismo mensaje con el parte completo (`notify.telegram_estado`, id en `tablero_msg`).
7. Si ya pasó `parte_diario_hora` y hoy no se mandó, envía el parte en silencio (`parte_dia` en el estado).

**Para replicarlo**

- Sin dependencias nuevas (usa `core.notify` y `core.config`). En el servidor: `./scripts/oracle_vigia.sh <host>`.
- `.env`: `TELEGRAM_BOT_TOKEN_VIGIA` y `TELEGRAM_CHAT_ID_VIGIA` (opcionales; si no, el bot general).
- Copiar `config/vigia.example.yaml` a `config/vigia.yaml` y probar con `--ver`.

### Adaptarlo a tu caso

- **Genérico:** auditar desde afuera con lo que cada servicio ya deja (estado en JSON, logs, systemd), avisar solo
  cambios de estado (problema nuevo / resuelto) y un parte diario. Sirve para cualquier conjunto de tareas
  programadas (cron, systemd, launchd).
- **Otro agente por corridas:** agregar una entrada con `servicio`, `log`, `log_max_min` y `frases_error`.
- **Un agente con estado propio:** escribir una función como `revisar_cita` que lea su archivo de estado y marcarla
  con `tipo:` en la config.
- **Otro canal:** cambiar `notify.telegram` en `avisar_cambios` y `main`.
- **Sin systemd (Mac):** los chequeos de servicio se saltan; el resto funciona con launchd (`instalar_launchd.sh vigia`).

## Decisiones de diseño (y por qué)

- **Botones de una lista cerrada:** desde Telegram solo se ejecutan las acciones de `picos.acciones` (tres tipos:
  `descargar_ollama`, `detener_proceso` por patrón, `pausar_cita`). El texto del botón nunca se ejecuta: si alguien
  tomara el bot, no podría correr comandos en el servidor. Además solo se atienden botones de tu chat.
- **Detener la tanda, no el bucle:** `detener_proceso` ignora shells (`bash -c "... patrón ..."`), así un bucle que
  reintenta (como el etiquetado) sigue vivo y solo se corta el proceso que consume.
- **Un pico es sostenido:** la CPU de la muestra *y* la carga de 1 min tienen que estar altas; un instante no avisa.
  Un aviso por episodio: suena al empezar, se edita mientras dura y se cierra tras 2 vueltas normales.

- **Desde afuera, sin tocar a los agentes:** si un agente se cuelga o su servicio cae, el que avisa no puede ser él
  mismo. El vigía corre aparte y solo lee.
- **Timer apagado = detenido a propósito:** apagar un agente (por ejemplo `empleo` mientras se entrena su modelo)
  no debe generar alarmas; aparece como "detenido" en el parte.
- **Un aviso por problema:** avisar en cada vuelta sería ruido y se terminaría ignorando. Se avisa al aparecer y al
  resolverse.
- **Reglas, no modelo:** son preguntas con respuesta exacta (¿pasó la hora?, ¿qué % falló?). Más adelante se puede
  sumar un resumen con LLM o acciones con aprobación (reiniciar un servicio).

## Uso

```bash
.venv/bin/python -m agents.vigia.run --ver     # muestra el parte sin avisar
.venv/bin/python -m agents.vigia.run --parte   # manda el parte ahora
./scripts/oracle_vigia.sh oracle               # instala en la VM (timer cada 2 min)
```

## Límites

- Solo ve lo que los agentes dejan en disco: si un agente "funciona" pero hace algo mal sin registrar error, no lo nota.
- Si el servidor entero se cae, el vigía cae con él: para eso hace falta un monitor externo.
