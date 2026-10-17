#!/bin/bash
# Vuelve a encender lo que apagó ./scripts/detener.sh: carga en launchd los agentes instalados
# y arranca Ollama como servicio de Homebrew.
# Uso: ./scripts/reanudar.sh
set -uo pipefail
cd "$(dirname "$0")/.."

AGENTS=(freelance fx empleo cita recordatorio)

for a in "${AGENTS[@]}"; do
  plist="$HOME/Library/LaunchAgents/com.agentic.$a.plist"
  [ -f "$plist" ] || continue  # agente no instalado (ej. freelance apagado)
  if launchctl print "gui/$(id -u)/com.agentic.$a" >/dev/null 2>&1; then
    echo "Agente $a ya estaba cargado"
  elif launchctl bootstrap "gui/$(id -u)" "$plist"; then
    echo "Agente $a cargado"
  fi
done

brew services start ollama >/dev/null && echo "Ollama encendido"
if [ -x .venv/bin/python ]; then  # empleo (LangGraph): avisa si al cargar retomará una corrida a medias
  echo "Empleo: $(.venv/bin/python -m agents.empleo.run --estado 2>&1 | tail -1)"
fi
echo "Listo. Revisa el estado con ./scripts/estado.sh"
