#!/bin/zsh
# Lleva el agente `empleo` a una VM de Oracle (Ubuntu 24.04, A1) desde la Mac, sin dejar claves en texto plano.
# Uso:   ./scripts/oracle_empleo.sh <host-ssh>     (alias de ~/.ssh/config o ubuntu@<ip>)
# Antes: la VM existe y tiene el repo (scripts/oracle_cita.sh o un git clone en ~/ai-personal-agents).
#
# Qué hace:
#  1. Actualiza el repo y las dependencias; instala Ollama (solo escucha en 127.0.0.1) para el modelo de respaldo.
#  2. Cifra GROQ_API_KEY, IMAP_PASSWORD y el bot de Telegram con systemd-creds (se descifran solo en memoria).
#  3. Copia config/empleo.yaml, config/profile.yaml y el historial (data/agentic.db, data/empleo_graph.db) con
#     permisos 600, y un .env solo con valores no secretos (IMAP_HOST, backends del modelo…).
#  4. Crea servicio + timer (cada 2 h), prueba las fuentes sin avisar y activa el timer si respondes que sí.
# El modelo de respaldo se copia aparte: ./experimentos/lora_mlx/exportar.sh servidor <host-ssh>
set -euo pipefail

VM="${1:?Uso: $0 <host-ssh>}"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
R=/home/ubuntu/ai-personal-agents
ssh_vm() { ssh "$VM" "$@"; }
valor() { "$PROJECT/.venv/bin/python" -c "import sys;from dotenv import dotenv_values;sys.stdout.write(dotenv_values('$PROJECT/.env').get('$1') or '')"; }

echo "==> 1. Repo, dependencias y Ollama"
ssh_vm "set -e; cd $R && git pull -q && .venv/bin/pip install -q -r requirements.txt
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh >/dev/null 2>&1
sudo systemctl enable --now ollama >/dev/null"

echo "==> 2. Claves cifradas con systemd-creds"
ssh_vm "sudo mkdir -p /etc/credstore.encrypted && sudo chmod 700 /etc/credstore.encrypted"
bot=$(valor TELEGRAM_BOT_TOKEN_EMPLEO); [ -n "$bot" ] || bot=$(valor TELEGRAM_BOT_TOKEN)
chat=$(valor TELEGRAM_CHAT_ID_EMPLEO); [ -n "$chat" ] || chat=$(valor TELEGRAM_CHAT_ID)
for pair in "empleo_groq:$(valor GROQ_API_KEY)" "empleo_imap:$(valor IMAP_PASSWORD)" "empleo_bot:$bot" "empleo_chat:$chat"; do
  name=${pair%%:*}
  printf '%s' "${pair#*:}" | ssh_vm "sudo systemd-creds encrypt --name=$name - /etc/credstore.encrypted/$name.cred 2>/dev/null"
done
ssh_vm 'sudo sh -c "chmod 600 /etc/credstore.encrypted/*.cred"'

echo "==> 3. Config, historial y .env sin secretos (permisos 600)"
ssh_vm "umask 077; mkdir -p $R/data"
scp -q "$PROJECT/config/empleo.yaml" "$PROJECT/config/profile.yaml" "$VM:$R/config/"
for f in agentic.db empleo_graph.db; do
  ssh_vm "[ -f $R/data/$f ]" || scp -q "$PROJECT/data/$f" "$VM:$R/data/"   # no pisa el historial de la VM
done
{
  for k in IMAP_HOST IMAP_USER IMAP_FOLDER GROQ_MODEL; do v=$(valor $k); [ -n "$v" ] && echo "$k=$v"; done
  echo "EMPLEO_SCORER_BACKEND=groq"
  echo "EMPLEO_SCORER_FALLBACK=ollama"
  echo "EMPLEO_OLLAMA_MODEL=${EMPLEO_OLLAMA_MODEL:-empleo-puntuador}"
  echo "OLLAMA_URL=http://127.0.0.1:11434"
} | ssh_vm "umask 077; cat > $R/.env.empleo"
ssh_vm "chmod 600 $R/config/empleo.yaml $R/config/profile.yaml $R/data/*.db $R/.env.empleo"

echo "==> 4. Servicio y timer (cada 2 h)"
ssh_vm "set -e
sudo tee /etc/systemd/system/agentic-empleo.service >/dev/null <<'EOF'
[Unit]
Description=Agente empleo (personal-agents)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$R
EnvironmentFile=$R/.env.empleo
# Claves cifradas con systemd-creds: se descifran solo en memoria, para este proceso
LoadCredentialEncrypted=empleo_groq:/etc/credstore.encrypted/empleo_groq.cred
LoadCredentialEncrypted=empleo_imap:/etc/credstore.encrypted/empleo_imap.cred
LoadCredentialEncrypted=empleo_bot:/etc/credstore.encrypted/empleo_bot.cred
LoadCredentialEncrypted=empleo_chat:/etc/credstore.encrypted/empleo_chat.cred
ExecStart=/bin/bash -c 'export GROQ_API_KEY=\"\$(cat \"\$CREDENTIALS_DIRECTORY/empleo_groq\")\" IMAP_PASSWORD=\"\$(cat \"\$CREDENTIALS_DIRECTORY/empleo_imap\")\" TELEGRAM_BOT_TOKEN_EMPLEO=\"\$(cat \"\$CREDENTIALS_DIRECTORY/empleo_bot\")\" TELEGRAM_CHAT_ID_EMPLEO=\"\$(cat \"\$CREDENTIALS_DIRECTORY/empleo_chat\")\"; exec .venv/bin/python -m agents.empleo.run \$EMPLEO_ARGS'
StandardOutput=append:$R/data/empleo.log
StandardError=append:$R/data/empleo.log
TimeoutStartSec=3600
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$R/data
EOF
sudo tee /etc/systemd/system/agentic-empleo.timer >/dev/null <<'EOF'
[Unit]
Description=Corre el agente empleo cada 2 h

[Timer]
OnBootSec=10min
OnUnitActiveSec=2h
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload"

echo "==> 5. Prueba de fuentes (no puntúa ni avisa)"
ssh_vm "n=\$(wc -l < $R/data/empleo.log 2>/dev/null || echo 0)
sudo systemctl set-environment EMPLEO_ARGS='--solo-fuentes'; sudo systemctl start agentic-empleo.service || true
sudo systemctl unset-environment EMPLEO_ARGS; tail -n +\$((n+1)) $R/data/empleo.log"
read "resp?¿Juntó ofertas sin errores? Activo el timer en la VM [s/N]: "
if [[ "$resp" == [sS] ]]; then
  ssh_vm "sudo systemctl enable --now agentic-empleo.timer"
  "$PROJECT/scripts/instalar_launchd.sh" empleo desinstalar 2>/dev/null || true
  echo "Listo: empleo corre en la VM cada 2 h. Log: ssh $VM tail -f $R/data/empleo.log"
else
  echo "El timer quedó sin activar. Revisa el log en la VM: $R/data/empleo.log"
fi
