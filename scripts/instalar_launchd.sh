#!/bin/zsh
# Programa un agente con launchd (macOS).
# Uso:   ./scripts/instalar_launchd.sh [agente]              -> instala (por defecto: freelance)
#        ./scripts/instalar_launchd.sh [agente] desinstalar
# Agentes: freelance (cada 2 h) · fx (cada 4 h) · empleo (cada 2 h) · cita (lanza cada 1 min; el ritmo de revisión lo aprende el agente) · recordatorio (diario, a las 9:00)
set -e

AGENT="${1:-freelance}"
case "$AGENT" in
  freelance) INTERVAL=7200 ;;
  fx)        INTERVAL=14400 ;;
  empleo)    INTERVAL=7200 ;;
  cita)      INTERVAL=60 ;;    # el agente decide si toca según el ritmo que aprendió (1-30 min)
  recordatorio) HOUR=9 ;;
  *) echo "Agente desconocido: $AGENT (opciones: freelance, fx, empleo, cita, recordatorio)"; exit 1 ;;
esac

LABEL="com.agentic.$AGENT"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$2" == "desinstalar" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Agente $AGENT desinstalado."
  exit 0
fi

if [[ -n "${HOUR:-}" ]]; then   # todos los días a una hora fija
  SCHEDULE="  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>0</integer></dict>"
  WHEN="todos los días a las $HOUR:00"
else
  SCHEDULE="  <key>StartInterval</key><integer>$INTERVAL</integer>"
  WHEN="cada $((INTERVAL / 60)) min"
fi

mkdir -p "$PROJECT/data"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT/.venv/bin/python</string>
    <string>-m</string>
    <string>agents.$AGENT.run</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
$SCHEDULE
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$PROJECT/data/$AGENT.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/data/$AGENT.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Agente $AGENT instalado: corre $WHEN. Log: $PROJECT/data/$AGENT.log"
