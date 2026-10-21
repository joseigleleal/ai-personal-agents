#!/bin/bash
# Deja la Mac descansando: saca de launchd los agentes programados, corta las corridas en curso
# y apaga Ollama (libera los ~5-6 GB del modelo). Al reiniciar o volver a iniciar sesión, launchd
# vuelve a cargar los agentes; Ollama solo con ./scripts/reanudar.sh.
# Uso: ./scripts/detener.sh
set -uo pipefail
cd "$(dirname "$0")/.."

AGENTS=(freelance fx empleo cita recordatorio vigia)

for a in "${AGENTS[@]}"; do
  if launchctl bootout "gui/$(id -u)/com.agentic.$a" 2>/dev/null; then echo "Agente $a detenido"; fi
done

pkill -f run_agent.sh 2>/dev/null && echo "Corridas en curso cortadas"
pkill -f "python.* -m agents\." 2>/dev/null
rm -f data/cita/corriendo  # candado de cita: si se cortó a mitad, que no bloquee la próxima corrida
# empleo (LangGraph) guarda su estado tras cada paso: si se cortó a mitad, la próxima corrida lo retoma
if [ -x .venv/bin/python ] && .venv/bin/python -m agents.empleo.run --estado 2>/dev/null | grep -q "a medias"; then
  echo "Empleo quedó a medias: se retoma solo en la próxima corrida (o empieza de cero con --reiniciar)"
fi

if brew services list | grep -q "^ollama .*started"; then
  brew services stop ollama >/dev/null && echo "Ollama apagado"
else
  pkill -x ollama 2>/dev/null && echo "Ollama apagado"
fi

if pgrep -f oracle_crear_vm.sh >/dev/null; then
  echo "Ojo: el reintento de Oracle sigue corriendo (no se toca). Para pararlo: pkill -f oracle_crear_vm.sh"
fi
echo "Listo. Para volver a encender todo: ./scripts/reanudar.sh"
