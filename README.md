# Personal Agents

Agentes autónomos de uso personal: trabajan en segundo plano y me avisan cuando hay algo que revisar.

## Cómo funciona

Cada agente es un programa de Python que se ejecuta solo cada cierto tiempo, hace su tarea, guarda el
resultado en una base SQLite local y avisa por Telegram si hay algo que revisar. Nunca envía ni paga nada
por su cuenta: propone y la persona decide.

```
programador (launchd en Mac / systemd en servidor)
   └─▶ scripts/run_agent.sh <agente>   git pull + python -m agents.<agente>.run
          └─▶ core/   config · SQLite · modelos de lenguaje · avisos
                 └─▶ Telegram + data/reports/ + data/agentic.db
```

**Base técnica**

| Pieza | Qué se usa |
|---|---|
| Lenguaje | Python 3.12, con entorno virtual en `.venv/` |
| Datos | SQLite (`data/agentic.db`): ofertas, tasas, avisos enviados, costos e ingresos |
| Modelos de lenguaje | A elección en `.env`: **Ollama** (local, gratis), **Groq** (API, plan gratis) o **Claude** (de pago, con tope mensual) |
| Avisos | Bot de Telegram (y notificación de macOS en la Mac) |
| Programación | `launchd` en la Mac · timers de `systemd` en el servidor |
| Servidor | Oracle Cloud Always Free (Ubuntu 24.04) |
| Despliegue | Repo público en GitHub: un `git push` actualiza el servidor en la siguiente corrida |
| Configuración | `.env` (secretos) y `config/profile.yaml` (perfil profesional), ambos solo locales y fuera de git |

**Agentes actuales**

- `freelance`: busca ofertas, las puntúa según el perfil y redacta borradores de propuesta (cada 2 h).
- `fx`: sigue la tasa EUR → COP y avisa cuando está barata frente a los últimos 90 días (cada 4 h). No usa modelo.
- `empleo`: busca empleo en España (Adzuna, InfoJobs y alertas por correo de LinkedIn/Indeed/Tecnoempleo) y puntúa según jornada, permiso de trabajo y modalidad (cada 6 h).

## Estructura

```
core/                 compartido entre agentes
  config.py           rutas, .env y perfil
  db.py               SQLite: ofertas, cotizaciones, avisos, costos, ingresos
  llm.py              Ollama (local), Groq (API gratis) y Claude (API, con tope de gasto)
  notify.py           avisos por Telegram y notificación de macOS
agents/freelance/     agente freelance asistido
  sources.py          fuentes: RemoteOK, Hacker News, Freelancer.com (API), alertas por correo
  scorer.py           pre-filtro por palabras + puntuación 0-10 con el modelo elegido
  writer.py           borrador de propuesta (en el idioma de la oferta)
  report.py           reporte diario en data/reports/ + notificación de macOS
  run.py              orquestador
agents/fx/            agente de tasa EUR → COP
  sources.py          TRM (Banco de la República) × EUR/USD (BCE) + control con open.er-api
  run.py              percentil frente a 30/90/365 días, avisos y margen de cada app
agents/empleo/        agente de empleo en España (sources, scorer, run)
config/              profile.example.yaml → profile.yaml (tu perfil) · empleo.example.yaml → empleo.yaml
scripts/              launchd (Mac), setup y timers de systemd (Oracle), creación de la VM
data/                 base de datos, reportes y logs (no se comparte)
```

## Puesta en marcha

```bash
cd ~/Desktop/personal-agents
cp .env.example .env            # por defecto todo gratis con Ollama
cp config/profile.example.yaml config/profile.yaml   # y completarlo con tu perfil
git config core.hooksPath scripts/hooks   # revisión antes de cada commit (ver "Repo público")
ollama pull qwen3:8b            # modelo local para puntuar y redactar (~5 GB)
ollama serve                    # si no está corriendo ya (o abrir la app de Ollama)

.venv/bin/python -m agents.freelance.run --solo-fuentes   # prueba las fuentes
.venv/bin/python -m agents.freelance.run --sin-propuestas  # solo puntúa
.venv/bin/python -m agents.freelance.run                  # corrida completa
```

### Elegir el modelo

En `.env`, `SCORER_BACKEND` (puntuar) y `WRITER_BACKEND` (redactar) aceptan `ollama`, `groq` o `claude`.
`SCORER_FALLBACK` y `WRITER_FALLBACK` definen un segundo backend por si el primero no está disponible o falla.
Groq necesita `GROQ_API_KEY` (gratis en console.groq.com) y sirve en servidores sin RAM para un modelo local.

Programarlo en la Mac: `./scripts/instalar_launchd.sh freelance` (cada 2 h) o `./scripts/instalar_launchd.sh fx` (cada 4 h).
Quitarlo: `./scripts/instalar_launchd.sh <agente> desinstalar`.

### Agente EUR → COP

```bash
.venv/bin/python -m agents.fx.run                            # avisa solo si hay señal de compra
.venv/bin/python -m agents.fx.run --resumen                  # resumen siempre
.venv/bin/python -m agents.fx.run --cotizacion global66 3720 # registra la tasa que ves en la app
```

Señal: la tasa de hoy está entre el 20 % más barato de los últimos 90 días (o en su mínimo), o bajó de
`FX_ALERTA_EUR_COP`. Cada domingo envía un resumen. No predice el futuro: compara con el historial.
Las tasas de las apps (Global66, bancos) se registran a mano: Global66 no tiene API pública y bloquea bots.

### Agente de empleo (España)

```bash
cp config/empleo.example.yaml config/empleo.yaml   # ciudades, jornada, permiso de trabajo (privado, fuera de git)
.venv/bin/python -m agents.empleo.run --solo-fuentes
.venv/bin/python -m agents.empleo.run
```

Fuentes: **Adzuna** (`ADZUNA_APP_ID/KEY`, gratis) e **InfoJobs** (`INFOJOBS_CLIENT_ID/SECRET`) por API oficial.
LinkedIn, Indeed y Tecnoempleo prohíben el scraping: se crean alertas en cada portal y el agente lee esos
correos por IMAP (sin marcarlos como leídos). Las habilidades salen de `profile.yaml`.

Reglas de puntaje: remoto o híbrido/presencial solo en `ciudades_presencial`; si la jornada supera
`permiso_trabajo.horas_max_semana` la oferta se avisa marcada ("requiere ajustar jornada"), salvo que
`jornadas` no incluya `completa`; las prácticas suman un punto; pedir nacionalidad UE baja el puntaje.
Nunca postula: avisa por Telegram y deja `data/reports/empleo-AAAA-MM-DD.md`.

## Despliegue en Oracle Cloud (Always Free)

```bash
# Desde la Mac: reintenta crear las VMs hasta que Oracle tenga capacidad (avisa al crearlas)
nohup caffeinate -i ./scripts/oracle_crear_vm.sh >/dev/null 2>&1 &   # micro (1 GB) y a1 (ARM, Ollama)

# En el servidor (Ubuntu 24.04). Con menos de 8 GB de RAM no instala Ollama y deja los backends en groq
git clone https://github.com/<usuario>/ai-personal-agents.git ~/personal-agents
cd ~/personal-agents && ./scripts/setup_oracle.sh   # instala todo y activa los timers (freelance 2 h, fx 4 h)
nano .env                                   # completar Telegram, correo, etc. (o copiarlo con scp)
# y copiar el perfil real desde la Mac: scp config/profile.yaml config/empleo.yaml ubuntu@<ip>:~/personal-agents/config/
sudo systemctl start agentic-freelance.service && journalctl -u agentic-freelance.service -f
```

Para actualizar el servidor basta con hacer `git push` desde la Mac: en cada corrida hace `git pull`.

## Flujo

1. Recolecta ofertas de las fuentes habilitadas (las que no tienen credenciales se saltan).
2. Descarta las ya vistas y las que no pasan el pre-filtro (gratis).
3. El modelo elegido (Ollama, Groq o Claude) puntúa de 0 a 10 según tu perfil.
4. Si el puntaje es >= `umbral_propuesta`, el modelo de `WRITER_BACKEND` redacta un borrador (máx. `MAX_PROPOSALS_PER_RUN`).
5. Todo queda en `data/reports/AAAA-MM-DD.md`. **Tú revisas y envías**; el agente nunca postula solo.

## Reglas

- Nada de scraping de Upwork/Workana: solo alertas por correo y APIs oficiales.
- El agente se detiene al llegar a `MONTHLY_BUDGET_USD`.
- `.env` y `data/` nunca se comparten.

## Repo público

Este repo es público y se trabaja directo en él. Los datos personales viven solo en la máquina:
`.env`, `config/profile.yaml` y `data/` están en `.gitignore`.
El hook `scripts/hooks/pre-commit` revisa cada commit y lo cancela si encuentra archivos privados o algo
que parezca un secreto (claves de API, tokens de Telegram, llaves privadas, correos). Para frenar también
nombres propios (empleadores, clientes), agregarlos uno por línea en `.secret-patterns` (archivo local, no se sube).
