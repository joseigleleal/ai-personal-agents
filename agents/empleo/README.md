# Agente `empleo` — ofertas de trabajo en España

**Qué hace:** junta ofertas de varias fuentes, descarta las que no encajan, las puntúa de 0 a 10 según el
perfil y las reglas personales (jornada, permiso de trabajo, modalidad, ciudad) y avisa las mejores.
**Nunca postula.**

| | |
|---|---|
| Tipo | Recolección + modelo de lenguaje para extraer datos + reglas en código |
| Frecuencia | Cada 2 h (correos en cada corrida; APIs cada `cada_horas_api` h) |
| Entradas | `config/profile.yaml` (habilidades) · `config/empleo.yaml` (preferencias privadas) · claves en `.env` |
| Salidas | Telegram (máx. 10 avisos por corrida) · `data/reports/empleo-AAAA-MM-DD.md` · SQLite |

## Flujo

```
fuentes ──▶ quitar ya vistas ──▶ pre-filtro por palabras (gratis) ──▶ modelo responde preguntas
        ──▶ el código calcula el puntaje ──▶ ≥ 7: aviso ✅ cumple permiso / 🟡 excede jornada ──▶ reporte
```

**Fuentes (solo canales permitidos):**
- APIs con clave: Adzuna, InfoJobs.
- APIs públicas sin clave: Get on Board, Remotive, Jobicy (solo remoto abierto a España, ≤ 14 días),
  bolsa de empleo universitaria (RSS) y datos abiertos de empleo público regional.
- Alertas por correo (IMAP, sin marcarlas como leídas): LinkedIn, Indeed, Tecnoempleo, InfoJobs.
  Estos portales prohíben el scraping, así que se crean alertas y el agente lee esos correos.

## Decisiones de diseño (y por qué)

- **El modelo extrae, el código decide.** Se le hacen preguntas concretas (¿remoto?, ¿horas?, ¿pide
  nacionalidad UE?) y el puntaje se calcula con reglas. Así un modelo chico y gratis no infla resultados.
- **Topes duros:** fuera de la ciudad y no remoto → descartada; pide nacionalidad UE → máx. 3; prácticas +1.
  Si la jornada supera el permiso de trabajo, se avisa marcada en vez de esconderla.
- **Ofertas con pocos datos** (solo título y ciudad, típico de las alertas): tope de 8 y "que lo revise una persona".
- **Modelo propio por agente:** `EMPLEO_SCORER_BACKEND=groq` con respaldo `ollama` si Groq falla o llega al límite.
- **Turnos para APIs:** se guardan en SQLite para no pasar los límites gratis.

## Uso

```bash
cp config/empleo.example.yaml config/empleo.yaml
.venv/bin/python -m agents.empleo.run --solo-fuentes   # prueba fuentes, no gasta turnos ni avisa
.venv/bin/python -m agents.empleo.run
./scripts/instalar_launchd.sh empleo
```
