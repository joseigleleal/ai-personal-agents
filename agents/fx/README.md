# Agente `fx` — tasa EUR → COP

**Qué hace:** calcula la tasa de mercado euro → peso colombiano y avisa cuando está barata frente a los
últimos 90 días. No predice: compara con el historial.

| | |
|---|---|
| Tipo | Datos públicos + estadística simple, sin modelo de lenguaje |
| Frecuencia | Cada 4 h; resumen los domingos |
| Entradas | `FX_MONTO_EUR`, `FX_ALERTA_EUR_COP` en `.env` · cotizaciones de apps registradas a mano |
| Salidas | Telegram · SQLite (`fx_quotes`, avisos enviados) |

## Flujo

```
TRM (datos.gov.co) × EUR/USD (BCE) ──▶ tasa EUR→COP diaria (control cruzado informativo con open.er-api)
   ──▶ percentil de hoy en 90 días ──▶ ≤ 20 o mínimo de 90 días o bajo el umbral ──▶ aviso de compra
```

## Cómo funciona por dentro

### En palabras simples

Quien compra euros con pesos de vez en cuando no sabe si el precio de hoy es bueno o malo. El agente hace lo
que haría una persona paciente con una libreta: anota el precio de cada día y lo compara con los últimos tres
meses. Si hoy está entre los más baratos, avisa; si no, se queda callado.

No adivina el futuro ni compra nada. Decide solo si el precio de hoy merece un aviso; cuándo y dónde comprar
lo decide la persona, con la ayuda del margen estimado de cada app que ella misma registra.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones/clases clave |
|---|---|---|
| `agents/fx/sources.py` | Descarga las series públicas y arma la tasa diaria | `_get`, `trm_history`, `eurusd_history`, `eurcop_history`, `eurcop_er_api` |
| `agents/fx/run.py` | Analiza, decide el aviso y registra cotizaciones | `analyze`, `percentile`, `provider_margins`, `summary`, `send`, `parse_rate`, `main` |
| `core/db.py` | SQLite compartida y avisos sin repetir | `connect`, `alert_once`, `now` |
| `core/notify.py` | Telegram con respaldo en macOS | `telegram`, `macos` |
| `core/config.py` | Lee `.env` y rutas | `env_float`, `DB_PATH` |

**Una corrida, paso a paso**

1. `main()` abre la base (`db.connect()`) y pide `sources.eurcop_history(400)` (400 días hacia atrás).
2. `trm_history` consulta `https://www.datos.gov.co/resource/32sa-8pi3.json` y repite cada valor en todo su rango
   `vigenciadesde`–`vigenciahasta` (cubre fines de semana y festivos).
3. `eurusd_history` consulta Frankfurter (`https://api.frankfurter.dev/v1/<desde>..?base=EUR&symbols=USD`).
4. `eurcop_history` cruza ambas: `EUR/COP = TRM × EUR/USD`; si un día no hay dato del BCE usa el último disponible.
5. `_get` reintenta solo ante errores de conexión o tiempo agotado (esperas de 5 y 20 s, `TIMEOUT = 20`). Si
   aun así falla, `main` termina con `sys.exit` y un mensaje corto.
6. `analyze` calcula nivel, promedios 30/90 d, rangos 90/365 d y variación de 7 días.
7. Con `--cotizacion`, guarda la tasa de la app junto a la de mercado en `fx_quotes` y termina.
8. Si no, arma el texto con `summary`, que agrega el margen por app y el control cruzado de
   `https://open.er-api.com/v6/latest/EUR` (solo informa si difiere más de 2 %; no cambia la decisión).
9. Decide con `db.alert_once` y envía con `send` (Telegram; si falla, notificación de macOS).

**Estado y datos** (en `data/agentic.db`)

| Tabla | Columnas principales | Uso |
|---|---|---|
| `fx_quotes` | `ts`, `provider`, `rate`, `mid` | Cotizaciones de apps; margen = promedio de `rate / mid − 1` por app |
| `alerts_sent` | `key`, `ts` | Claves `fx:<nivel>:<día>`, `fx:umbral:<día>`, `fx:semanal:<día>` |

El historial de tasas no se guarda: se descarga completo en cada corrida.

**Reglas y cálculos que deciden** (el porqué está en "Decisiones de diseño")

- Percentil = % de los últimos 90 valores estrictamente menores que el de hoy (0 = el más barato).
- Nivel: `excelente` si es el mínimo de 90 días; `buena` si percentil ≤ `BUENA` (20); `cara` si ≥ `CARA` (80);
  si no, `normal`. Solo `excelente`/`buena` generan aviso.
- Umbral fijo: aviso si `FX_ALERTA_EUR_COP` > 0 y la tasa queda en o bajo ese valor (por defecto `0`, apagado).
- Ahorro mostrado: `(promedio 90 d − tasa de hoy) × FX_MONTO_EUR` (por defecto `1000`).
- Resumen semanal: la primera corrida del domingo. `--resumen` lo fuerza.

**Para replicarlo**

- Dependencias: `requests`, `python-dotenv`, `pyyaml` (de `requirements.txt`). No usa credenciales para los datos.
- `.env`: `FX_MONTO_EUR`, `FX_ALERTA_EUR_COP`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (o sus variantes
  `_FX` para un bot propio).
- Pasos: copiar `.env.example` a `.env`, correr `python -m agents.fx.run --resumen` y programarlo cada 4 h
  (`scripts/instalar_launchd.sh fx`).

### Adaptarlo a tu caso

- **Genérico (se reutiliza tal cual):** el patrón serie diaria → percentil en su historial → aviso en el extremo
  bueno; `analyze`, `percentile`, `alert_once`, `send` y el registro manual de cotizaciones con margen.
- **Específico:** EUR→COP, la TRM de Colombia, Frankfurter como fuente de EUR/USD y las apps de envío.
- **Otro par de monedas:** reescribir `sources.py` para que devuelva `dict[date, float]`; si hay tasa directa,
  basta una fuente sin cruce. Cambiar textos de `summary` y renombrar las variables `FX_*`.
- **Criptomonedas:** misma función de fuente con una API de precios diarios; conviene ventana más corta y
  revisar `BUENA`/`CARA`, porque la volatilidad es mayor.
- **Producto o combustible:** la fuente puede ser un dato abierto o una lectura propia guardada a diario
  (aquí habría que agregar una tabla de historial, porque hoy no se persiste).
- Si lo bueno es un precio **alto** (vender), invertir la comparación en `analyze`.

## Decisiones de diseño (y por qué)

- **Sin modelo:** es un cálculo; un modelo solo agregaría costo y errores.
- **Margen de las apps:** Global66 bloquea bots y no tiene API, así que su tasa se registra a mano
  (`--cotizacion global66 <tasa>`) y el agente calcula su margen sobre el mercado.
- **Un aviso por señal:** la tabla `alerts_sent` evita repetir el mismo aviso.
- **Reintentos de red:** las APIs públicas a veces tardan; la TRM y el BCE se reintentan 2 veces (a los 5 y
  20 s) ante errores de conexión o tiempo agotado. El control con open.er-api no se reintenta: si falla, se omite.
  Si sigue fallando, la corrida termina con una línea en el log (sin traceback) y lo intenta en la próxima.

## Uso

```bash
.venv/bin/python -m agents.fx.run                              # avisa solo si hay señal
.venv/bin/python -m agents.fx.run --resumen                    # resumen siempre
.venv/bin/python -m agents.fx.run --cotizacion global66 3720   # registra la tasa de una app
```
