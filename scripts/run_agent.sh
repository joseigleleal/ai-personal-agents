#!/bin/bash
# Actualiza el código desde git y corre un agente.
# Uso: ./scripts/run_agent.sh <agente> [argumentos]   (agentes: freelance, fx, empleo)
# Lo usan los timers de systemd en el servidor; también sirve en la Mac.
set -uo pipefail
cd "$(dirname "$0")/.."

AGENT="${1:?Uso: run_agent.sh <agente> [argumentos]}"
shift
if [ -d .git ]; then
  git pull --ff-only --quiet || echo "git pull falló; sigo con la versión actual"
fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python -m "agents.$AGENT.run" "$@"
