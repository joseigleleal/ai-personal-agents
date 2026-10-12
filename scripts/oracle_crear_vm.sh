#!/bin/bash
# Reintenta crear las VMs Always Free en Oracle Cloud hasta que haya capacidad.
# Un solo proceso alterna entre las VMs pendientes: Oracle limita los pedidos por usuario,
# así que dos procesos en paralelo se bloquean entre sí ("Too many requests").
# Corre en la Mac con la OCI CLI configurada (~/.oci/config).
# Uso:  ./scripts/oracle_crear_vm.sh [micro] [a1]   (por defecto: micro a1)
#       nohup caffeinate -i ./scripts/oracle_crear_vm.sh >/dev/null 2>&1 &   (en segundo plano)
#   micro: VM.Standard.E2.1.Micro (AMD, 1 GB) "agents-micro", para agentes sin modelo local o con Groq.
#   a1:    VM.Standard.A1.Flex (ARM, 4/24 o 2/12) "personal-agents", para Ollama.
set -uo pipefail
export PYTHONWARNINGS=ignore SUPPRESS_LABEL_WARNING=True

SUBNET_NAME="public subnet-vcn-agents"
SSH_PUB="$HOME/.ssh/oracle_agents.pub"
PAUSE=60              # segundos entre pedidos
INTERVAL=300          # segundos entre rondas
LOG="$(cd "$(dirname "$0")/.." && pwd)/data/oracle_vm.log"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
notify() { osascript -e "display notification \"$1\" with title \"Oracle VM\" sound name \"Glass\"" 2>/dev/null; }

# Cada candidato: objetivo nombre shape ocpu mem (en orden de preferencia, todo dentro de Always Free)
CANDIDATES=()
[ $# -eq 0 ] && set -- micro a1
for target in "$@"; do
  case "$target" in
    micro) CANDIDATES+=("micro agents-micro VM.Standard.E2.1.Micro 1 1") ;;
    a1)    CANDIDATES+=("a1 personal-agents VM.Standard.A1.Flex 4 24" "a1 personal-agents VM.Standard.A1.Flex 2 12") ;;
    *)     echo "Uso: $0 [micro] [a1]"; exit 1 ;;
  esac
done

TENANCY=$(awk -F= '/^tenancy/{print $2; exit}' "$HOME/.oci/config")
AD=$(oci iam availability-domain list --compartment-id "$TENANCY" --query 'data[0].name' --raw-output)
SUBNET=$(oci network subnet list --compartment-id "$TENANCY" --display-name "$SUBNET_NAME" --query 'data[0].id' --raw-output)
image_for() {  # la Micro es x86 y la A1 es ARM: cada una necesita su imagen
  oci compute image list --compartment-id "$TENANCY" --operating-system "Canonical Ubuntu" \
    --operating-system-version "24.04" --shape "$1" --sort-by TIMECREATED --query 'data[0].id' --raw-output
}
IMAGE_MICRO=$(image_for VM.Standard.E2.1.Micro)
IMAGE_A1=$(image_for VM.Standard.A1.Flex)

if [ -z "$AD" ] || [ -z "$SUBNET" ] || [ -z "$IMAGE_MICRO" ] || [ -z "$IMAGE_A1" ]; then
  log "ERROR: no encontré AD, subred o imagen. Revisa ~/.oci/config y la VCN."; exit 1
fi
log "Inicio ($*). AD=$AD · Ubuntu 24.04 · subred '$SUBNET_NAME'"

existing() {
  oci compute instance list --compartment-id "$TENANCY" --display-name "$1" \
    --query "data[?\"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='TERMINATING'].id | [0]" --raw-output 2>/dev/null
}

report_ip() {
  local name=$1 id=$2 ip=""
  log "[$name] Instancia creada: $id. Esperando IP pública..."
  for _ in $(seq 1 30); do
    ip=$(oci compute instance list-vnics --instance-id "$id" --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
    [ -n "$ip" ] && [ "$ip" != "null" ] && break
    sleep 10
  done
  log "[$name] LISTO. IP pública: ${ip:-desconocida} → ssh -i ~/.ssh/oracle_agents ubuntu@$ip"
  notify "$name creada. IP: ${ip:-ver consola}"
}

DONE=" "
attempt=0
while true; do
  wait=$INTERVAL
  for c in "${CANDIDATES[@]}"; do
    read -r target name shape ocpu mem <<<"$c"
    [[ "$DONE" == *" $name "* ]] && continue
    id=$(existing "$name")
    if [ -n "$id" ] && [ "$id" != "null" ]; then report_ip "$name" "$id"; DONE+="$name "; continue; fi

    attempt=$((attempt + 1))
    config=()
    image=$IMAGE_MICRO
    if [ "$target" = a1 ]; then
      config=(--shape-config "{\"ocpus\": $ocpu, \"memoryInGBs\": $mem}"); image=$IMAGE_A1
    fi
    out=$(oci compute instance launch \
      --availability-domain "$AD" --compartment-id "$TENANCY" \
      --display-name "$name" --shape "$shape" ${config[@]+"${config[@]}"} \
      --image-id "$image" --subnet-id "$SUBNET" --assign-public-ip true \
      --ssh-authorized-keys-file "$SSH_PUB" \
      --query 'data.id' --raw-output 2>&1)
    if [[ "$out" == ocid1.instance.* ]]; then
      log "Intento $attempt [$name $ocpu/$mem]: OK"
      report_ip "$name" "$out"; DONE+="$name "
    elif grep -qiE "capacity|InternalError" <<<"$out"; then
      log "Intento $attempt [$name $ocpu/$mem]: sin capacidad"
    elif grep -qiE "TooManyRequests|429" <<<"$out"; then
      log "Intento $attempt [$name $ocpu/$mem]: demasiados pedidos, espero el doble"
      wait=$((INTERVAL * 2)); break
    elif grep -qiE "timed out|RequestException|ServiceUnavailable|Connection(Error| reset| aborted)" <<<"$out"; then
      log "Intento $attempt [$name $ocpu/$mem]: falla de red, reintento en la próxima vuelta"
      break
    else
      log "Intento $attempt [$name $ocpu/$mem]: error inesperado, me detengo:"
      log "$(grep -E '"(code|message)"' <<<"$out" | tr -s ' ')"
      notify "Error inesperado al crear la VM (ver data/oracle_vm.log)"
      exit 1
    fi
    sleep "$PAUSE"
  done

  pending=0
  for c in "${CANDIDATES[@]}"; do
    read -r _ name _ <<<"$c"; [[ "$DONE" == *" $name "* ]] || pending=1
  done
  if [ "$pending" = 0 ]; then log "Todas las VMs creadas."; exit 0; fi
  sleep "$wait"
done
