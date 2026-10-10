#!/bin/bash
# Muestra en una pantalla qué está corriendo en la Mac: agentes programados, reintento de Oracle,
# Ollama, configuración (sin mostrar secretos) y estado de git.
# Uso: ./scripts/estado.sh
set -uo pipefail
cd "$(dirname "$0")/.."

AGENTS=(freelance fx empleo)
ok()   { printf "  \033[32m●\033[0m %s\n" "$1"; }
off()  { printf "  \033[90m○\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m▲\033[0m %s\n" "$1"; }
title() { printf "\n\033[1m%s\033[0m\n" "$1"; }
since() {  # hace cuánto se modificó un archivo
  local s=$(( $(date +%s) - $(stat -f %m "$1") ))
  if   [ $s -lt 3600 ];  then echo "hace $((s / 60)) min"
  elif [ $s -lt 86400 ]; then echo "hace $((s / 3600)) h"
  else echo "hace $((s / 86400)) d"; fi
}

title "Agentes programados (launchd)"
for agent in "${AGENTS[@]}"; do
  line="$(launchctl list | awk -v l="com.agentic.$agent" '$3 == l')"
  log="data/$agent.log"
  last=""; [ -f "$log" ] && last=" · última corrida $(since "$log")"
  if [ -z "$line" ]; then
    off "$agent: no programado"
    continue
  fi
  pid="$(awk '{print $1}' <<<"$line")"; code="$(awk '{print $2}' <<<"$line")"
  if [ "$pid" != "-" ]; then ok "$agent: corriendo ahora (PID $pid)$last"
  elif [ "$code" = "0" ]; then ok "$agent: programado, última salida OK$last"
  else warn "$agent: programado, última salida con error ($code)$last · ver $log"; fi
done

title "Reintento de VMs en Oracle"
copies="$(pgrep -f "^/bin/bash ./scripts/oracle_crear_vm.sh" | while read -r p; do
  [ "$(ps -o ppid= -p "$p" | tr -d ' ')" = "1" ] && echo "$p"; done)"
n="$(grep -c . <<<"$copies")"
if [ "$n" = 0 ]; then off "detenido"
elif [ "$n" = 1 ]; then ok "corriendo (PID $copies, desde hace $(ps -o etime= -p "$copies" | tr -d ' '))"
else warn "$n copias corriendo (PIDs $(tr '\n' ' ' <<<"$copies")): deja solo una, Oracle bloquea por pedidos"; fi
[ -f data/oracle_vm.log ] && tail -2 data/oracle_vm.log | sed 's/^/    /'

title "Ollama"
if models="$(curl -s --max-time 3 http://localhost:11434/api/tags)"; then
  ok "activo · modelos: $(grep -o '"name":"[^"]*"' <<<"$models" | cut -d'"' -f4 | tr '\n' ' ')"
else
  off "no responde (brew services start ollama)"
fi

title "Configuración (.env, sin mostrar valores)"
has() { grep -qE "^$1=.+" .env 2>/dev/null; }
for pair in "Telegram:TELEGRAM_BOT_TOKEN" "Correo IMAP:IMAP_USER" "Groq:GROQ_API_KEY" "Claude:ANTHROPIC_API_KEY" \
            "Adzuna:ADZUNA_APP_ID" "InfoJobs:INFOJOBS_CLIENT_ID"; do
  if has "${pair#*:}"; then ok "${pair%%:*}"; else off "${pair%%:*}: sin configurar"; fi
done
for f in config/profile.yaml config/empleo.yaml; do
  if [ -f "$f" ]; then ok "$f"; else warn "$f no existe (se usa el de ejemplo)"; fi
done

title "Git"
branch="$(git rev-parse --abbrev-ref HEAD)"
changes="$(git status --porcelain | grep -c .)"
git fetch -q origin 2>/dev/null
ahead="$(git rev-list --count "origin/$branch..HEAD" 2>/dev/null || echo "?")"
info="$branch · último commit: $(git log -1 --format='%h %s (%cr)')"
if [ "$changes" = 0 ] && [ "$ahead" = 0 ]; then ok "$info"
else warn "$info · $changes archivo(s) sin commit · $ahead commit(s) sin push"; fi
echo
