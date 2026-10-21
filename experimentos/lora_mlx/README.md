# Experimento: LoRA con MLX — un modelo chico que puntúa ofertas como uno grande

**Qué hace:** entrena con LoRA un modelo de 1,5B parámetros (Qwen2.5-1.5B-Instruct) en una Mac con Apple
Silicon para que haga la extracción de datos que el agente `empleo` hoy le pide a un modelo grande en la nube
(Groq gpt-oss-120b). El resultado se cuantiza a 4 bits, se sirve con Ollama en la Mac o en un servidor Linux sin
GPU y el agente lo puede usar como modelo local.

| | |
|---|---|
| Tipo | Destilación: un modelo grande ("maestro") etiqueta, uno chico ("alumno") aprende a imitarlo |
| Hardware | Mac con Apple Silicon y 16 GB (entrenamiento ~6 GB de memoria); servidor ARM sin GPU para servirlo |
| Entradas | Ofertas guardadas por `empleo` (SQLite) · perfil (`config/profile.yaml`) · `GROQ_API_KEY` |
| Salidas | `data/lora/`: etiquetas, datos, adaptador, evaluaciones · modelo `empleo-puntuador` en Ollama |

**Estado: en curso.** El código y el flujo están listos y probados con un entrenamiento corto; falta terminar
de etiquetar el dataset con el maestro, entrenar y publicar aquí los resultados de la evaluación.

## Flujo

```
ofertas guardadas ──▶ maestro (Groq) responde el mismo JSON que usa el agente ──▶ etiquetas.jsonl
   ──▶ train / valid / test (80/10/10) ──▶ LoRA con mlx-lm (solo se entrena la respuesta)
   ──▶ evaluación contra el maestro en test ──▶ fusionar + cuantizar (q4_K_M) ──▶ Ollama (Mac o servidor)
```

## Decisiones de diseño (y por qué)

- **Destilación sobre una tarea real del repo:** no hay que etiquetar a mano; el maestro produce el dataset y la
  tarea (extraer un JSON fijo) es acotada, justo donde un modelo chico afinado puede acercarse a uno grande.
- **Mismo prompt que en producción** (`scorer.build_prompt` y `SCORER_SYSTEM`): el modelo afinado se puede
  enchufar al agente sin cambiar nada más (`EMPLEO_OLLAMA_MODEL`).
- **Se evalúa lo que importa al agente**, no solo la pérdida: JSON válido, acierto por campo, puntaje final con
  las mismas reglas de `agents/empleo/scorer.py` y si coincide la decisión de avisar. Se compara contra el modelo
  base sin LoRA para medir lo que aporta el entrenamiento.
- **LoRA sobre el modelo sin cuantizar (bf16)** y cuantización recién al final: entrenar en 4 bits (QLoRA) ahorra
  memoria, pero con 1,5B no hace falta en 16 GB y la fusión queda más simple.
- **`mask_prompt`:** el prompt (perfil + oferta) es largo y siempre igual en estructura; la pérdida se calcula solo
  sobre la respuesta, que es lo que se quiere aprender.
- **Ollama para servir:** importa los safetensors fusionados, cuantiza y corre igual en macOS y en Linux ARM sin GPU.
  El GGUF resultante (~1 GB) se copia al servidor; allá no hace falta MLX ni Python.
- **Datos fuera de git:** etiquetas y prompts incluyen el perfil personal; todo queda en `data/lora/`.

## Uso

```bash
.venv/bin/pip install -r experimentos/lora_mlx/requirements.txt          # mlx-lm (solo Apple Silicon)

# 1. Dataset (se puede cortar y retomar; con Groq gratis, ~3 ofertas por minuto)
.venv/bin/python -u -m experimentos.lora_mlx.dataset --etiquetar
.venv/bin/python -m experimentos.lora_mlx.dataset --separar

# 2. Línea base, entrenamiento y evaluación
.venv/bin/python -m experimentos.lora_mlx.evaluar --nombre base
.venv/bin/python -m mlx_lm lora -c experimentos/lora_mlx/lora.yaml
.venv/bin/python -m experimentos.lora_mlx.evaluar --nombre lora --adaptador data/lora/adaptador

# 3. Exportar a Ollama y probar el modelo cuantizado
./experimentos/lora_mlx/exportar.sh ollama
.venv/bin/python -m experimentos.lora_mlx.evaluar --nombre ollama --ollama empleo-puntuador

# 4. Usarlo en el agente: en .env
EMPLEO_OLLAMA_MODEL=empleo-puntuador
```

### En un servidor Linux sin GPU (ej. VM ARM de Oracle Cloud)

```bash
ssh oracle 'curl -fsSL https://ollama.com/install.sh | sh'   # Ollama queda como servicio en 127.0.0.1:11434
./experimentos/lora_mlx/exportar.sh servidor oracle            # copia el GGUF y crea el modelo allá
ssh -N -L 11435:localhost:11434 oracle &                       # túnel: el puerto no se abre a internet
OLLAMA_URL=http://localhost:11435 .venv/bin/python -m experimentos.lora_mlx.evaluar --nombre servidor --ollama empleo-puntuador
```

## Resultados

Pendiente: se completa al terminar el etiquetado y el entrenamiento.

## Límites

- MLX solo corre en Apple Silicon; en Linux o Windows con NVIDIA, lo equivalente es PEFT/Unsloth con CUDA.
- El alumno aprende a imitar al maestro, errores incluidos: el techo de calidad es el maestro.
- Unos cientos de ejemplos alcanzan para el formato y los criterios de esta tarea, no para tareas abiertas.
- Una MacBook Air no tiene ventilador: en entrenamientos largos baja el rendimiento por temperatura.
