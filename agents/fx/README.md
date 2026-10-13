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
TRM (datos.gov.co) × EUR/USD (BCE) ──▶ tasa EUR→COP diaria (control cruzado con open.er-api)
   ──▶ percentil de hoy en 90 días ──▶ ≤ 20 o mínimo de 90 días o bajo el umbral ──▶ aviso de compra
```

## Decisiones de diseño (y por qué)

- **Sin modelo:** es un cálculo; un modelo solo agregaría costo y errores.
- **Margen de las apps:** Global66 bloquea bots y no tiene API, así que su tasa se registra a mano
  (`--cotizacion global66 <tasa>`) y el agente calcula su margen sobre el mercado.
- **Un aviso por señal:** la tabla `alerts_sent` evita repetir el mismo aviso.
- **Reintentos de red:** las APIs públicas a veces tardan; cada consulta se reintenta 2 veces (a los 5 y 20 s).
  Si sigue fallando, la corrida termina con una línea en el log (sin traceback) y lo intenta en la próxima.

## Uso

```bash
.venv/bin/python -m agents.fx.run                              # avisa solo si hay señal
.venv/bin/python -m agents.fx.run --resumen                    # resumen siempre
.venv/bin/python -m agents.fx.run --cotizacion global66 3720   # registra la tasa de una app
```
