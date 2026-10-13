# Agente `freelance` — ofertas freelance con borrador de propuesta

**Qué hace:** busca proyectos freelance, los puntúa según el perfil y, para los mejores, redacta un
borrador de propuesta en el idioma de la oferta. **Nunca envía nada:** la persona revisa y postula.
(Hoy está apagado por decisión personal; el código está completo.)

| | |
|---|---|
| Tipo | Recolección + modelo para puntuar + modelo para redactar |
| Frecuencia | Cada 2 h |
| Entradas | `config/profile.yaml` · `.env` (modelos, IMAP, token de Freelancer.com) |
| Salidas | `data/reports/AAAA-MM-DD.md` · Telegram · SQLite (ofertas, costos) |

## Flujo

```
RemoteOK · Hacker News ("Seeking freelancer", "Who is hiring") · Freelancer.com (API) · alertas por correo
   ──▶ quitar ya vistas ──▶ pre-filtro por palabras ──▶ puntaje 0-10
   ──▶ ≥ umbral_propuesta: borrador (inglés o español) ──▶ reporte + aviso
```

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
