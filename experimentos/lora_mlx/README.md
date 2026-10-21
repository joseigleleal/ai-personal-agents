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

## Cómo funciona por dentro

### En palabras simples

El agente `empleo` le manda cada oferta a un modelo enorme en la nube y le pide un JSON con datos (rol, modalidad,
horas, señales de estafa…). Funciona, pero depende de internet, tiene límites de uso y un modelo así no se puede
tener en casa. La idea es enseñarle esa tarea concreta a un modelo unas 80 veces más chico que sí corre en una Mac
o en un servidor sin GPU.

Se hace como con un maestro y un alumno. El maestro (el modelo grande) resuelve cientos de ofertas reales con el
mismo pedido que usa el agente, y eso queda como un libro de ejercicios resueltos, sin escribir ninguno a mano. El
alumno (el chico) practica con ese libro hasta responder igual. Un modelo chico no puede saber todo lo que sabe uno
grande, pero sí aprender muy bien una sola tarea con formato fijo. Al final se mide con ejercicios que el alumno
nunca vio si toma las mismas decisiones que el maestro.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones / claves |
|---|---|---|
| `dataset.py` | Etiqueta con el maestro (se retoma donde quedó) y separa en train/valid/test | `etiquetar`, `separar` |
| `lora.yaml` | Configuración del entrenamiento para `mlx_lm lora` | `rank`, `mask_prompt`, `iters`… |
| `evaluar.py` | Compara al alumno (MLX, con o sin adaptador, u Ollama) contra el maestro en test | `evaluar`, `generador_mlx`, `generador_ollama` |
| `exportar.sh` | Fusiona, cuantiza y crea el modelo en Ollama; opcional, lo copia a un servidor | `ollama`, `servidor <host>` |
| `agents/empleo/scorer.py` | Prompt y reglas de producción que se reutilizan tal cual | `build_prompt`, `SCORER_SYSTEM` |

**Qué es afinar.** Un modelo es una gran cantidad de números (pesos) en matrices, capa tras capa. Entrenar es
repetir: darle un ejemplo, medir cuánto se aleja de la respuesta correcta (la **pérdida**), calcular hacia dónde
mover cada peso para que baje (el **gradiente**) y moverlos un poco (la **tasa de aprendizaje**). Ajustar todos los
pesos de un modelo de 1,5B pide guardar en memoria los pesos, sus gradientes y el estado del optimizador: varias
veces el tamaño del modelo.

**El truco de LoRA** (*Low-Rank Adaptation*). Congela cada matriz original *W* y aprende solo una corrección de bajo
rango, *ΔW = B·A*. En una matriz de atención de Qwen2.5-1.5B (1536×1536) con rango *r* = 16, *B* es 1536×16 y *A*
16×1536: se entrenan ~49 mil números en vez de ~2,36 millones (≈ 2 %). Funciona porque el cambio que pide una tarea
concreta vive en pocas direcciones. El resultado (el **adaptador**) pesa pocos MB y al final se **fusiona**:
*W' = W + B·A*, un modelo normal sin pasos extra al usarlo.

**La configuración** (`lora.yaml`):

| Parámetro | Valor | Para qué |
|---|---|---|
| `model` | `Qwen/Qwen2.5-1.5B-Instruct` | Alumno que ya sabe seguir instrucciones |
| `lora_parameters.rank` | 16 | Cuántas direcciones puede aprender: más capacidad, más riesgo de memorizar |
| `lora_parameters.scale` | 20 | Cuánto pesa la corrección *B·A* frente a *W* |
| `lora_parameters.dropout` | 0,05 | Apaga al azar parte de la corrección en cada paso para que no memorice |
| `num_layers` | -1 | LoRA en todas las capas |
| `mask_prompt` | true | La pérdida se calcula solo sobre la respuesta: el prompt es entrada, no se aprende |
| `batch_size` · `grad_accumulation_steps` | 1 · 4 | Lote efectivo de 4 con la memoria de 1 |
| `iters` · `learning_rate` | 600 · 1e-4 | ~1,7 pasadas sobre ~350 ejemplos, con pasos chicos |
| `max_seq_length` | 2560 | Prompt + respuesta, en tokens |
| `grad_checkpoint` | true | Recalcula en vez de guardar: más lento, menos memoria (~6 GB en total) |

**MLX** es la librería de Apple para entrenar en sus chips; usa la memoria unificada (compartida entre CPU y GPU),
por eso alcanza una Mac de 16 GB.

**Cómo se sabe si aprendió.**
- Separación 80/10/10: entrenamiento, **validación** (durante el entrenamiento) y **prueba** (solo al final; el
  alumno nunca la ve). Si la pérdida de entrenamiento baja y la de validación sube, está memorizando (*overfitting*);
  por eso se guarda el adaptador cada 100 pasos.
- `evaluar.py` mide lo que le importa al agente: JSON válido y completo, acierto por campo, puntaje con las mismas
  reglas del agente y si coincide la decisión de avisar.
- **Línea base:** el mismo alumno sin LoRA. La diferencia entre base y LoRA es lo que aportó el entrenamiento.

**Llevarlo a producción.** `exportar.sh ollama` fusiona el adaptador (`mlx_lm fuse`) y Ollama importa el modelo
y lo **cuantiza** a 4 bits (`q4_K_M`): cada peso pasa de 16 bits a ~4 y el modelo baja de ~3 GB a ~1 GB con poca
pérdida, que se vuelve a medir con `evaluar.py --ollama`. `exportar.sh servidor <host>` copia ese archivo (GGUF) a un
servidor Linux con Ollama, sin MLX ni Python. En el agente se activa con `EMPLEO_OLLAMA_MODEL`.

### Adaptarlo a tu caso

- **Genérico (sirve tal cual):** destilar una tarea acotada de un modelo grande a uno chico — etiquetar con el
  maestro usando el prompt de producción, entrenar LoRA con `mask_prompt`, evaluar con métricas de la tarea contra
  una línea base, cuantizar y servir con Ollama.
- **Otra tarea:** cambiar en `dataset.py` de dónde salen los ejemplos (`ofertas`) y qué prompt se arma, y en
  `evaluar.py` qué campos se comparan y qué decisión importa. Conviene que la salida tenga formato fijo (JSON).
- **Otro maestro o alumno:** el maestro es cualquier modelo al que se llegue por API (en `etiquetar`); el alumno se
  cambia en `model:` de `lora.yaml` (Llama, Gemma, Phi…) y en `BASE` de `exportar.sh`. Más grande, mejor techo y
  más memoria.
- **Sin Mac:** en Linux o Windows con GPU NVIDIA, lo equivalente es PEFT o Unsloth; el dataset (JSONL con mensajes)
  y la evaluación se reutilizan.
- **Si sobreajusta o no aprende:** bajar `iters` o `rank`, o subir `dropout`; si no aprende, más ejemplos o más
  `iters`.

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
- En equipos con antivirus o control de archivos, la primera vez que Python carga una librería grande
  (`anthropic`, `mlx`) puede tardar varios minutos sin usar CPU; las siguientes cargas son inmediatas.
