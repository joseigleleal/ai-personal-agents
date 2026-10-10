#!/bin/bash
# Prepara una VM de Oracle Cloud (Ubuntu 24.04) para correr los agentes.
# Uso (en el servidor, dentro del repo clonado):  ./scripts/setup_oracle.sh
# En la A1 (24 GB) instala Ollama. En la Micro (1 GB) no hay RAM para un modelo local: se salta Ollama,
# se agrega swap y el agente freelance debe usar Groq (SCORER_BACKEND=groq, WRITER_BACKEND=groq en .env).
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
MODEL="${OLLAMA_MODEL:-qwen3:8b}"
MEM_GB=$(awk '/MemTotal/{print int($2/1024/1024)}' /proc/meminfo)
WITH_OLLAMA=$([ "$MEM_GB" -ge 8 ] && echo 1 || echo 0)

echo "==> Paquetes del sistema y actualizaciones de seguridad automáticas"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip git curl unattended-upgrades fail2ban
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

if [ "$WITH_OLLAMA" = 1 ]; then
  echo "==> Ollama (escucha solo en 127.0.0.1, no se expone a internet)"
  if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  sudo systemctl enable --now ollama
  ollama pull "$MODEL"
else
  echo "==> RAM de ${MEM_GB} GB: sin Ollama. Swap de 2 GB para que pip y Python no se queden sin memoria"
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
    sudo mkswap /swapfile && sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
fi

echo "==> Entorno de Python"
cd "$PROJECT"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    Se creó .env desde .env.example: complétalo con 'nano .env'"
  if [ "$WITH_OLLAMA" = 0 ]; then
    sed -i 's/^SCORER_BACKEND=.*/SCORER_BACKEND=groq/; s/^WRITER_BACKEND=.*/WRITER_BACKEND=groq/' .env
    echo "    Sin Ollama: .env quedó con SCORER_BACKEND=groq y WRITER_BACKEND=groq (falta GROQ_API_KEY)"
  fi
fi
chmod 600 .env
chmod +x scripts/*.sh

echo "==> Timers de systemd (un servicio y un timer por agente)"
install_agent() {  # $1 = agente, $2 = intervalo (ej: 2h)
  local agent=$1 every=$2
  sudo tee "/etc/systemd/system/agentic-$agent.service" >/dev/null <<EOF
[Unit]
Description=Agente $agent (personal-agents)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$PROJECT
ExecStart=$PROJECT/scripts/run_agent.sh $agent
TimeoutStartSec=3600
Nice=10
EOF

  sudo tee "/etc/systemd/system/agentic-$agent.timer" >/dev/null <<EOF
[Unit]
Description=Corre el agente $agent cada $every

[Timer]
OnBootSec=5min
OnUnitActiveSec=$every
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

install_agent freelance 2h
install_agent fx 4h
sudo systemctl daemon-reload
sudo systemctl enable --now agentic-freelance.timer agentic-fx.timer

echo
echo "Listo. Comandos útiles (cambia 'freelance' por 'fx' para el otro agente):"
echo "  systemctl list-timers 'agentic-*'                  # próximas corridas"
echo "  sudo systemctl start agentic-freelance.service     # correr ahora"
echo "  journalctl -u agentic-freelance.service -f         # ver logs"
