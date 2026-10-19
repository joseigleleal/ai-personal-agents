#!/bin/zsh
# Lleva el agente `cita` a una VM de Oracle (Ubuntu 24.04, A1) desde la Mac, sin dejar contraseñas en texto plano.
# Uso:   ./scripts/oracle_cita.sh ubuntu@<ip> [~/.ssh/llave]
# Antes: la VM existe (scripts/oracle_crear_vm.sh) y tiene la llave SSH. No hace falta correr setup_oracle.sh.
#
# Qué hace:
#  1. Instala git, Python, Chromium (con sus librerías y fuentes), fail2ban y actualizaciones automáticas.
#  2. Cifra CITA_CERT_PASSWORD y el bot de Telegram con systemd-creds (se descifran solo en memoria al correr).
#  3. Copia certificado, config/cita.yaml y la memoria del agente con permisos 600.
#  4. Endurece SSH (sin root, solo el usuario ubuntu), pone la hora de España y crea servicio + timer (cada 1 min).
#  5. Quita `cita` de launchd en la Mac: el sitio no tolera dos sesiones de la misma persona a la vez.
set -euo pipefail

VM="${1:?Uso: $0 ubuntu@<ip> [llave]}"
KEY="${2:-$HOME/.ssh/oracle_agents}"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_URL="$(git -C "$PROJECT" remote get-url origin)"
R=/home/ubuntu/ai-personal-agents
ssh_vm() { ssh -i "$KEY" "$VM" "$@"; }

echo "==> 1. Paquetes, repo y Chromium en la VM"
ssh_vm "set -e
sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git unattended-upgrades fail2ban >/dev/null
[ -d $R ] || git clone -q $REPO_URL $R
cd $R && git pull -q && python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium >/dev/null"

echo "==> 2. Claves cifradas con systemd-creds"
ssh_vm "sudo mkdir -p /etc/credstore.encrypted && sudo chmod 700 /etc/credstore.encrypted"
for pair in cita_cert:CITA_CERT_PASSWORD cita_bot:TELEGRAM_BOT_TOKEN_CITA cita_chat:TELEGRAM_CHAT_ID; do
  name=${pair%%:*}; var=${pair##*:}
  "$PROJECT/.venv/bin/python" -c "import sys;from dotenv import dotenv_values;sys.stdout.write(dotenv_values('$PROJECT/.env').get('$var') or '')" \
    | ssh_vm "sudo systemd-creds encrypt --name=$name - /etc/credstore.encrypted/$name.cred 2>/dev/null"
done
ssh_vm 'sudo sh -c "chmod 600 /etc/credstore.encrypted/*.cred"'

echo "==> 3. Certificado, config y memoria (permisos 600)"
ssh_vm "umask 077; mkdir -p $R/data/cita && chmod 700 $R/data $R/data/cita"
scp -q -i "$KEY" "$PROJECT/data/cita/certificado.p12" "$VM:$R/data/cita/"
[ -f "$PROJECT/data/cita/aprendizaje.json" ] && scp -q -i "$KEY" "$PROJECT/data/cita/aprendizaje.json" "$VM:$R/data/cita/"
scp -q -i "$KEY" "$PROJECT/config/cita.yaml" "$VM:$R/config/"
ssh_vm "chmod 600 $R/data/cita/* $R/config/cita.yaml"

echo "==> 4. SSH, hora, servicio y timer"
ssh_vm "set -e
printf 'PermitRootLogin no\nAllowUsers ubuntu\nMaxAuthTries 3\n' | sudo tee /etc/ssh/sshd_config.d/99-endurecido.conf >/dev/null
sudo sshd -t && sudo systemctl reload ssh
sudo timedatectl set-timezone Europe/Madrid
sudo tee /etc/systemd/system/agentic-cita.service >/dev/null <<'EOF'
[Unit]
Description=Agente cita (personal-agents)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$R
# Claves cifradas con systemd-creds: se descifran solo en memoria, para este proceso
LoadCredentialEncrypted=cita_cert:/etc/credstore.encrypted/cita_cert.cred
LoadCredentialEncrypted=cita_bot:/etc/credstore.encrypted/cita_bot.cred
LoadCredentialEncrypted=cita_chat:/etc/credstore.encrypted/cita_chat.cred
ExecStart=/bin/bash -c 'export CITA_CERT_PASSWORD=\"\$(cat \"\$CREDENTIALS_DIRECTORY/cita_cert\")\" TELEGRAM_BOT_TOKEN_CITA=\"\$(cat \"\$CREDENTIALS_DIRECTORY/cita_bot\")\" TELEGRAM_CHAT_ID_CITA=\"\$(cat \"\$CREDENTIALS_DIRECTORY/cita_chat\")\"; exec .venv/bin/python -m agents.cita.run \$CITA_ARGS'
StandardOutput=append:$R/data/cita.log
StandardError=append:$R/data/cita.log
TimeoutStartSec=1200
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$R/data /home/ubuntu/.cache
EOF
sudo tee /etc/systemd/system/agentic-cita.timer >/dev/null <<'EOF'
[Unit]
Description=Lanza el agente cita cada 1 min (el agente decide si toca revisar)

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload"

echo "==> 5. Prueba sin reservar (con cita detenida en la Mac)"
launchctl bootout "gui/$(id -u)/com.agentic.cita" 2>/dev/null || true
ssh_vm "n=\$(wc -l < $R/data/cita.log 2>/dev/null || echo 0)
sudo systemctl set-environment CITA_ARGS='--ya --solo-revisar'; sudo systemctl start agentic-cita.service || true
sudo systemctl unset-environment CITA_ARGS; tail -n +\$((n+1)) $R/data/cita.log"
read "resp?¿Llegó a 'Sin citas disponibles'? Activo el timer en la VM y quito cita de la Mac [s/N]: "
if [[ "$resp" == [sS] ]]; then
  ssh_vm "sudo systemctl enable --now agentic-cita.timer"
  "$PROJECT/scripts/instalar_launchd.sh" cita desinstalar
  echo "Listo: cita corre en la VM. Log: ssh -i $KEY $VM tail -f $R/data/cita.log"
else
  "$PROJECT/scripts/instalar_launchd.sh" cita
  echo "cita sigue en la Mac. Revisa las capturas en la VM: $R/data/cita/ultima/"
fi
