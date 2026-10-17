# Agente `empleo` — ofertas de trabajo en España

**Qué hace:** junta ofertas de varias fuentes, descarta las que no encajan, las puntúa de 0 a 10 según el
perfil y las reglas personales (jornada, permiso de trabajo, modalidad, ciudad) y avisa las mejores. Si una
oferta excede las horas que permite el permiso de trabajo, propone una nota corta para postular.
**Nunca postula.** Es el **piloto con LangGraph** del repo: ver [docs/langgraph.md](../../docs/langgraph.md).

| | |
|---|---|
| Tipo | Grafo de LangGraph: recolección + modelo para extraer datos + reglas en código + modelo redactor |
| Frecuencia | Cada 2 h (correos en cada corrida; APIs cada `cada_horas_api` h) |
| Entradas | `config/profile.yaml` (habilidades) · `config/empleo.yaml` (preferencias privadas) · claves en `.env` |
| Salidas | Telegram (máx. 10 avisos por corrida) · `data/reports/empleo-AAAA-MM-DD.md` · SQLite · `data/empleo_graph.db` (estado del grafo) |

## Flujo

```
recolectar ─▶ prefiltrar ─▶ puntuar ⟲ (lotes de 10) ─▶ priorizar ─▶ [redactar_notas] ─▶ reportar
```

Cada oferta puntuada queda en un grupo según el permiso de trabajo (`permiso_trabajo.horas_max_semana`):

| Grupo | Cuándo | Se avisa |
|---|---|---|
| ✅ Cumple tu permiso | Jornada parcial, ≤ horas máximas o prácticas | Con puntaje ≥ `umbral_aviso` (+1 punto) |
| 🟢 Por proyecto / freelance | Contrato por proyecto, B2B o autónomo: se pacta la dedicación | ≥ `umbral_aviso`, con nota para postular |
| 🤝 Jornada negociable | Jornada completa, pero dice horario flexible, jornada reducida o semana de 4 días | ≥ `umbral_aviso`, con nota |
| ❔ Jornada sin indicar | El anuncio no lo dice (típico de las alertas por correo) | ≥ `umbral_aviso`, con nota |
| 🟡 Excede tu permiso | Jornada completa sin flexibilidad | Solo si es excelente (`umbral_excede`, máx. `max_avisos_excede`) y −1 punto. El reporte las tiene todas |

**Fuentes (solo canales permitidos):**
- APIs con clave: Adzuna (con una pasada extra solo de jornada parcial, `buscar_parcial`) e InfoJobs.
- APIs públicas sin clave: Get on Board, Remotive, Jobicy (solo remoto abierto a España, ≤ 14 días),
  bolsa de empleo universitaria (RSS) y datos abiertos de empleo público regional.
- Alertas por correo (IMAP, sin marcarlas como leídas): LinkedIn, Indeed, Tecnoempleo, InfoJobs.
  Estos portales prohíben el scraping, así que se crean alertas y el agente lee esos correos.
  Consejo: crea alertas con el filtro de **media jornada** y **prácticas**, no solo las generales.

## Decisiones de diseño (y por qué)

- **El modelo extrae, el código decide.** Se le hacen preguntas concretas (¿remoto?, ¿horas?, ¿contrato por
  proyecto?, ¿horario flexible?) y el puntaje se calcula con reglas. Así un modelo chico y gratis no infla
  resultados. Lo que se puede saber por el título (país, puesto de dirección) lo decide una expresión regular.
- **El permiso de trabajo ordena todo.** Con muchas ofertas remotas de jornada completa, avisar todo lo que
  "encaja" llenaba Telegram de ofertas que luego descartaban por las horas. Ahora se avisa primero lo que se
  puede aceptar tal cual y lo que se puede negociar, con una nota que pone las horas por delante.
- **LangGraph con checkpointer.** El estado se guarda tras cada paso. Si la corrida se corta (sin red, Groq
  caído, Mac dormida), la siguiente la retoma; las ofertas que no alcanzaron a puntuarse quedan pendientes.
- **Topes duros:** fuera de la ciudad y no remoto → máx. 2; remoto solo para residentes de otro país → se
  descarta si el título lo dice, máx. 2 si lo detecta el modelo en el anuncio; dirección de equipos → máx. 4; pide nacionalidad UE → máx. 3; prácticas +1.
- **Ofertas con pocos datos** (solo título y ciudad, típico de las alertas): tope de 8 y "revísalo en el anuncio".
- **Modelo propio por agente:** `EMPLEO_SCORER_BACKEND=groq` con respaldo `ollama` (`with_fallbacks`),
  temperatura 0 para que el mismo anuncio dé el mismo resultado.

## Uso

```bash
cp config/empleo.example.yaml config/empleo.yaml
.venv/bin/python -m agents.empleo.run --solo-fuentes   # prueba fuentes, no gasta turnos ni avisa
.venv/bin/python -m agents.empleo.run                  # corrida real
.venv/bin/python -m agents.empleo.run --estado         # ¿quedó una corrida a medias?
.venv/bin/python -m unittest tests.test_empleo -v      # pruebas sin red ni claves
./scripts/instalar_launchd.sh empleo
```

## Límites

- Jornada y horas salen del texto del anuncio: si no las dice, queda en ❔ y hay que mirarlo.
- La nota para postular es un borrador: revísala antes de usarla.
- Confirma las condiciones de tu permiso de trabajo con la fuente oficial; el agente solo aplica el número de
  horas que le das.
