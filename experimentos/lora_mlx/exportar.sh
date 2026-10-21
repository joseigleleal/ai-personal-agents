#!/usr/bin/env bash
# Lleva el LoRA entrenado a Ollama (en la Mac) y, opcional, a un servidor Linux por SSH.
#
#   ./experimentos/lora_mlx/exportar.sh ollama            # fusiona adaptador + base y crea el modelo cuantizado
#   ./experimentos/lora_mlx/exportar.sh servidor oracle   # copia el modelo a ese host (alias de ~/.ssh/config)
#
# Variables: NOMBRE (empleo-puntuador), CUANT (q4_K_M), BASE (Qwen/Qwen2.5-1.5B-Instruct).
set -euo pipefail
cd "$(dirname "$0")/../.."

NOMBRE="${NOMBRE:-empleo-puntuador}"
CUANT="${CUANT:-q4_K_M}"
BASE="${BASE:-Qwen/Qwen2.5-1.5B-Instruct}"
DIR="data/lora"
PARAMS=$'PARAMETER temperature 0\nPARAMETER num_ctx 4096'   # el prompt del puntuador ronda los 2000 tokens

case "${1:-}" in
  ollama)
    # 1. Fusionar: los pesos del LoRA se suman al modelo base → un modelo normal (bf16, safetensors)
    .venv/bin/python -m mlx_lm fuse --model "$BASE" --adapter-path "$DIR/adaptador" --save-path "$DIR/fusionado"
    # 2. Ollama importa los safetensors y cuantiza a 4 bits (~1 GB); la plantilla de chat sale del tokenizer
    printf 'FROM ./fusionado\n%s\n' "$PARAMS" > "$DIR/Modelfile"
    (cd "$DIR" && ollama create "$NOMBRE" --quantize "$CUANT" -f Modelfile)
    ollama show "$NOMBRE" | head -15
    ;;
  servidor)
    HOST="${2:?Falta el host, ej.: servidor oracle}"
    # El modelo de Ollama es un GGUF dentro de su carpeta de blobs: se copia ese archivo y se recrea allá
    GGUF=$(ollama show "$NOMBRE" --modelfile | awk '/^FROM \//{print $2; exit}')
    PLANTILLA=$(ollama show "$NOMBRE" --template)
    ssh "$HOST" 'command -v ollama >/dev/null' || {
      echo "Falta Ollama en $HOST. Instalar con: ssh $HOST 'curl -fsSL https://ollama.com/install.sh | sh'"; exit 1; }
    ssh "$HOST" "mkdir -p ~/modelos"
    rsync -h --progress "$GGUF" "$HOST:modelos/$NOMBRE.gguf"
    printf 'FROM ./%s.gguf\nTEMPLATE """%s"""\n%s\n' "$NOMBRE" "$PLANTILLA" "$PARAMS" \
      | ssh "$HOST" "cat > modelos/Modelfile && cd modelos && ollama create $NOMBRE -f Modelfile"
    ssh "$HOST" "ollama list | grep $NOMBRE"
    echo "Listo. Desde la Mac: ssh -N -L 11435:localhost:11434 $HOST  →  OLLAMA_URL=http://localhost:11435"
    ;;
  *)
    sed -n '2,7p' "$0"; exit 1 ;;
esac
