#!/usr/bin/env bash
# Lleva el LoRA entrenado a Ollama (en la Mac) y, opcional, a un servidor Linux por SSH.
#
#   ./experimentos/lora_mlx/exportar.sh ollama            # fusiona, convierte a GGUF, cuantiza y crea el modelo
#   ./experimentos/lora_mlx/exportar.sh servidor oracle   # copia el GGUF a ese host (alias de ~/.ssh/config)
#
# Variables: NOMBRE (empleo-puntuador), CUANT (Q5_K_M), BASE (Qwen/Qwen2.5-1.5B-Instruct), PESADO (carpeta con
# llama.cpp y su venv; por defecto data/lora/pesado.nosync, fuera de iCloud).
# Requisitos (una vez): brew install llama.cpp   (trae llama-quantize)
#   git clone --depth 1 https://github.com/ggml-org/llama.cpp "$PESADO/llama.cpp"
#   python3 -m venv "$PESADO/venv-gguf" && "$PESADO/venv-gguf/bin/pip" install -r "$PESADO/llama.cpp/requirements/requirements-convert_hf_to_gguf.txt"
set -euo pipefail
cd "$(dirname "$0")/../.."

NOMBRE="${NOMBRE:-empleo-puntuador}"
CUANT="${CUANT:-Q5_K_M}"
BASE="${BASE:-Qwen/Qwen2.5-1.5B-Instruct}"
DIR="data/lora"
PESADO="${PESADO:-$DIR/pesado.nosync}"
SALIDA="$PESADO/export"
GGUF="$SALIDA/$CUANT.gguf"

modelfile() {  # plantilla de chat de Qwen (ChatML), determinista y con contexto para el prompt (~2000 tokens)
  printf 'FROM ./%s\nTEMPLATE """{{- range .Messages }}<|im_start|>{{ .Role }}\n{{ .Content }}<|im_end|>\n{{ end }}<|im_start|>assistant\n"""\nPARAMETER stop "<|im_end|>"\nPARAMETER temperature 0\nPARAMETER num_ctx 4096\n' "$1"
}

case "${1:-}" in
  ollama)
    mkdir -p "$SALIDA"
    # 1. Fusionar: los pesos del LoRA se suman al modelo base → un modelo normal (safetensors)
    .venv/bin/python -m mlx_lm fuse --model "$BASE" --adapter-path "$DIR/adaptador" --save-path "$SALIDA/fusionado"
    # transformers 5 (el de mlx_lm) guarda los tokens especiales como lista en «extra_special_tokens»; el conversor
    # de llama.cpp los espera en «additional_special_tokens». Mismos tokens, otro nombre.
    .venv/bin/python - "$SALIDA/fusionado/tokenizer_config.json" <<'PY'
import json, sys
ruta = sys.argv[1]; c = json.load(open(ruta))
if isinstance(c.get("extra_special_tokens"), list):
    c["additional_special_tokens"] = c.pop("extra_special_tokens")
    json.dump(c, open(ruta, "w"), ensure_ascii=False, indent=2)
PY
    # 2. GGUF en 16 bits y cuantizado (Ollama ya no cuantiza al importar safetensors)
    "$PESADO/venv-gguf/bin/python" "$PESADO/llama.cpp/convert_hf_to_gguf.py" "$SALIDA/fusionado" \
      --outfile "$SALIDA/f16.gguf" --outtype f16
    llama-quantize "$SALIDA/f16.gguf" "$GGUF" "$CUANT" > "$SALIDA/quantize.log" 2>&1
    # 3. Modelo en Ollama
    modelfile "$CUANT.gguf" > "$SALIDA/Modelfile"
    (cd "$SALIDA" && ollama create "$NOMBRE" -f Modelfile)
    ls -lh "$GGUF"
    ;;
  servidor)
    HOST="${2:?Falta el host, ej.: servidor oracle}"
    [ -f "$GGUF" ] || { echo "No existe $GGUF: corre antes «$0 ollama»."; exit 1; }
    ssh "$HOST" 'command -v ollama >/dev/null' || {
      echo "Falta Ollama en $HOST. Instalar con: ssh $HOST 'curl -fsSL https://ollama.com/install.sh | sh'"; exit 1; }
    ssh "$HOST" "mkdir -p ~/modelos"
    rsync -h --progress "$GGUF" "$HOST:modelos/$NOMBRE.gguf"
    modelfile "$NOMBRE.gguf" | ssh "$HOST" "cat > modelos/Modelfile-$NOMBRE && cd modelos && ollama create $NOMBRE -f Modelfile-$NOMBRE"
    ssh "$HOST" "ollama list | grep $NOMBRE"
    echo "Listo. Desde la Mac: ssh -N -L 11435:localhost:11434 $HOST  →  OLLAMA_URL=http://localhost:11435"
    ;;
  *)
    sed -n '2,11p' "$0"; exit 1 ;;
esac
