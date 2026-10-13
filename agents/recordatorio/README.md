# Agente `recordatorio` — pendientes del día

**Qué hace:** cada mañana manda por Telegram la lista de pendientes de configuración y los pasos del
siguiente. Sirve para no olvidar tareas que solo puede hacer la persona (crear cuentas, claves, alertas).

| | |
|---|---|
| Tipo | Lectura de un archivo de notas, sin modelo de lenguaje |
| Frecuencia | Diario a las 9:00 (solo en la Mac) |
| Entradas | Notas locales fuera de git (`memoria.md`, o `PENDIENTES_FILE` en `.env`) |
| Salidas | Un mensaje de Telegram al día como máximo |

## Cómo funciona

- Busca la sección "Pendientes del usuario" y en ella los títulos `### N. [ ] ...` con sus pasos.
- Da por hecho un pendiente cuando su clave ya está en `.env` (ej. "Groq" → `GROQ_API_KEY`), aunque no se
  haya marcado `[x]`.
- Envía la lista y los pasos completos del primero.

## Decisión de diseño

Las notas las mantiene Claude Code en cada sesión (ver `CLAUDE.md`), así que el recordatorio siempre refleja
el estado real sin otra base de datos.

## Uso

```bash
.venv/bin/python -m agents.recordatorio.run --ver      # solo muestra el mensaje
.venv/bin/python -m agents.recordatorio.run --forzar   # envía aunque ya avisó hoy
```
