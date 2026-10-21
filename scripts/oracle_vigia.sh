#!/bin/zsh
# Instala el vigía (audita a los demás agentes) en la VM de Oracle, con el bot de Telegram cifrado.
# Uso:   ./scripts/oracle_vigia.sh <host-ssh> [config]   (alias de ~/.ssh/config o ubuntu@<ip>; config por defecto:
#        config/vigia.yaml). Con varias VMs, una config por VM; los botones de picos solo en una (comparten bot).
# Antes: la VM tiene el repo en ~/ai-personal-agents (scripts/oracle_cita.sh). Usa el bot TELEGRAM_BOT_TOKEN_VIGIA
# o, si no existe, el general (TELEGRAM_BOT_TOKEN), y config/vigia.yaml (o el .example si no lo creaste).
set -euo pipefail

VM="${1:?Uso: $0 <host-ssh> [config]}"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
R=/home/ubuntu/ai-personal-agents
ssh_vm() { ssh "$VM" "$@"; }
valor() { "$PROJECT/.venv/bin/python" -c "import sys;from dotenv import dotenv_values;sys.stdout.write(dotenv_values('$PROJECT/.env').get('$1') or '')"; }

echo "==> 1. Repo al día y bot cifrado con systemd-creds"
ssh_vm "cd $R && git pull -q && sudo mkdir -p /etc/credstore.encrypted && sudo chmod 700 /etc/credstore.encrypted"
bot=$(valor TELEGRAM_BOT_TOKEN_VIGIA); [ -n "$bot" ] || bot=$(valor TELEGRAM_BOT_TOKEN)
chat=$(valor TELEGRAM_CHAT_ID_VIGIA); [ -n "$chat" ] || chat=$(valor TELEGRAM_CHAT_ID)
for pair in "vigia_bot:$bot" "vigia_chat:$chat"; do
  printf '%s' "${pair#*:}" | ssh_vm "sudo systemd-creds encrypt --name=${pair%%:*} - /etc/credstore.encrypted/${pair%%:*}.cred 2>/dev/null"
done
ssh_vm 'sudo sh -c "chmod 600 /etc/credstore.encrypted/vigia_*.cred"'
CFG="${2:-$PROJECT/config/vigia.yaml}"
[ -f "$CFG" ] && scp -q "$CFG" "$VM:$R/config/vigia.yaml" && ssh_vm "chmod 600 $R/config/vigia.yaml"

echo "==> 2. Servicio y timer (cada 2 min)"
ssh_vm "set -e
sudo tee /etc/systemd/system/agentic-vigia.service >/dev/null <<'EOF'
[Unit]
Description=Vigía: audita a los demás agentes (personal-agents)

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$R
LoadCredentialEncrypted=vigia_bot:/etc/credstore.encrypted/vigia_bot.cred
LoadCredentialEncrypted=vigia_chat:/etc/credstore.encrypted/vigia_chat.cred
ExecStart=/bin/bash -c 'export TELEGRAM_BOT_TOKEN_VIGIA=\"\$(cat \"\$CREDENTIALS_DIRECTORY/vigia_bot\")\" TELEGRAM_CHAT_ID_VIGIA=\"\$(cat \"\$CREDENTIALS_DIRECTORY/vigia_chat\")\"; exec .venv/bin/python -m agents.vigia.run'
StandardOutput=append:$R/data/vigia.log
StandardError=append:$R/data/vigia.log
TimeoutStartSec=300
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$R/data
EOF
sudo tee /etc/systemd/system/agentic-vigia.timer >/dev/null <<'EOF'
[Unit]
Description=Corre el vigía cada 2 min (atiende los botones de los picos)

[Timer]
OnBootSec=5min
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now agentic-vigia.timer"
ssh_vm "cd $R && .venv/bin/python -m agents.vigia.run --ver"
echo "Listo. Log: ssh $VM tail -f $R/data/vigia.log"
