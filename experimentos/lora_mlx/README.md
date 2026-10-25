# Experimento: LoRA con MLX — un modelo chico que puntúa ofertas como uno grande

**Qué hace:** entrena con LoRA un modelo de 1,5B parámetros (Qwen2.5-1.5B-Instruct) en una Mac con Apple
Silicon para que haga la extracción de datos que el agente `empleo` hoy le pide a un modelo grande en la nube
(Groq gpt-oss-120b). El resultado se convierte a GGUF, se cuantiza a 5 bits y se sirve con Ollama en la Mac o en un
servidor Linux sin GPU. Ahí el agente lo usa como puntuador principal, con el modelo grande comparando en modo sombra.

| | |
|---|---|
| Tipo | Destilación: un modelo grande ("maestro") etiqueta, uno chico ("alumno") aprende a imitarlo |
| Hardware | Mac con Apple Silicon y 16 GB (entrenamiento ~6 GB de memoria); servidor ARM sin GPU para servirlo |
| Entradas | Ofertas guardadas por `empleo` (SQLite) · perfil (`config/profile.yaml`) · `GROQ_API_KEY` |
| Salidas | `data/lora/`: etiquetas, datos, adaptador, evaluaciones · modelo `empleo-puntuador` en Ollama |

**Estado: en uso, en modo sombra.** Ronda 2 entrenada con 456 ofertas. El modelo q5 corre en un servidor ARM sin
GPU como puntuador del agente `empleo`, con Groq en paralelo: en los desacuerdos manda Groq, y sus respuestas
alimentan la próxima ronda (ver [Resultados](#resultados)).

## Flujo

```
ofertas guardadas ──▶ maestro (Groq) responde el mismo JSON que usa el agente ──▶ etiquetas.jsonl
   ──▶ train / valid / test (80/10/10) ──▶ LoRA con mlx-lm (solo se entrena la respuesta)
   ──▶ evaluación contra el maestro en test ──▶ fusionar ──▶ GGUF (llama.cpp) ──▶ cuantizar (Q5_K_M) ──▶ Ollama
   ──▶ agente en modo sombra: el alumno decide, el maestro compara y sus respuestas vuelven a etiquetas.jsonl
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
| `exportar.sh` | Fusiona, convierte a GGUF con llama.cpp, cuantiza y crea el modelo en Ollama; opcional, lo copia a un servidor | `ollama`, `servidor <host>`, `CUANT` |
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
| `lora_parameters.scale` | 10 | Cuánto pesa la corrección *B·A* frente a *W* (con 20 divergió) |
| `lora_parameters.dropout` | 0,05 | Apaga al azar parte de la corrección en cada paso para que no memorice |
| `num_layers` | -1 | LoRA en todas las capas |
| `mask_prompt` | true | La pérdida se calcula solo sobre la respuesta: el prompt es entrada, no se aprende |
| `batch_size` · `grad_accumulation_steps` | 1 · 4 | Lote efectivo de 4 con la memoria de 1 |
| `iters` · `learning_rate` | 360 · 3e-5 | ~1 pasada sobre los ejemplos, con pasos chicos (1e-4 divergió) |
| `max_seq_length` | 2560 | Prompt + respuesta, en tokens. `dataset.py --separar` descarta de train/valid lo que no cabe |
| `grad_checkpoint` | true | Recalcula en vez de guardar: más lento, menos memoria (~6 GB en total) |

**MLX** es la librería de Apple para entrenar en sus chips; usa la memoria unificada (compartida entre CPU y GPU),
por eso alcanza una Mac de 16 GB.

**Cómo se sabe si aprendió.**
- Separación 80/10/10: entrenamiento, **validación** (durante el entrenamiento) y **prueba** (solo al final; el
  alumno nunca la ve). Si la pérdida de entrenamiento baja y la de validación sube, está memorizando (*overfitting*);
  por eso se valida y se guarda el adaptador cada 50 pasos.
- `evaluar.py` mide lo que le importa al agente: JSON válido y completo, acierto por campo, puntaje con las mismas
  reglas del agente y si coincide la decisión de avisar.
- **Línea base:** el mismo alumno sin LoRA. La diferencia entre base y LoRA es lo que aportó el entrenamiento.

**Llevarlo a producción.** `exportar.sh ollama` fusiona el adaptador (`mlx_lm fuse`), convierte a GGUF en 16 bits
con el conversor de llama.cpp y lo **cuantiza** con `llama-quantize` (por defecto `Q5_K_M`: cada peso pasa de 16
bits a ~5 y el modelo baja de ~3 GB a ~1 GB). Las versiones nuevas de Ollama ya no cuantizan al importar
safetensors, por eso se usa llama.cpp. La pérdida se vuelve a medir con `evaluar.py --ollama`.
`exportar.sh servidor <host>` copia el GGUF a un servidor Linux con Ollama, sin MLX ni Python. En el agente:
`EMPLEO_OLLAMA_MODEL` y, para estrenarlo sin riesgo, el modo sombra (`EMPLEO_SOMBRA=groq`, ver la ficha de
[empleo](../../agents/empleo/README.md)).

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
- **GGUF + Ollama para servir:** llama.cpp convierte y cuantiza una vez en la Mac; Ollama corre el mismo archivo
  (~1 GB) en macOS y en Linux ARM sin GPU. En el servidor no hace falta MLX ni Python.
- **Q5_K_M y no Q4_K_M:** pesa 0,1 GB más y mantiene la decisión del modelo en 16 bits; con 4 bits ya se pierden
  decisiones (ver Resultados).
- **Modo sombra antes de soltarlo:** el set de prueba es chico (46) y las ofertas reales cambian. Comparar con el
  maestro en producción mide lo que importa y, de paso, sigue juntando ejemplos etiquetados.
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

# 4. Usarlo en el agente (.env): el LoRA decide y Groq compara en modo sombra
EMPLEO_SCORER_BACKEND=ollama
EMPLEO_SCORER_FALLBACK=groq
EMPLEO_OLLAMA_MODEL=empleo-puntuador
EMPLEO_SOMBRA=groq

# 5. Reentrenar con lo que juntó la sombra (traer data/lora/etiquetas.jsonl del servidor si corre allá)
.venv/bin/python -m experimentos.lora_mlx.dataset --separar      # ¡cambia el set de prueba! comparar con la base otra vez
```

### En un servidor Linux sin GPU (ej. VM ARM de Oracle Cloud)

```bash
ssh oracle 'curl -fsSL https://ollama.com/install.sh | sh'   # Ollama queda como servicio en 127.0.0.1:11434
./experimentos/lora_mlx/exportar.sh servidor oracle            # copia el GGUF y crea el modelo allá
ssh -N -L 11435:localhost:11434 oracle &                       # túnel: el puerto no se abre a internet
OLLAMA_URL=http://localhost:11435 .venv/bin/python -m experimentos.lora_mlx.evaluar --nombre servidor --ollama empleo-puntuador
```

## Resultados

**Ronda 1 (2026-10-21)**, con 182 de 435 ofertas etiquetadas: el plan gratis de Groq tiene un tope de 200.000
tokens por día (~90 ofertas), así que se entrenó con lo que había. 145 de entrenamiento, 18 de validación y 19 de
prueba. Alumno `Qwen2.5-1.5B-Instruct`, Mac M4 de 16 GB.

| Métrica (19 ofertas de prueba, contra el maestro) | Base, sin LoRA | LoRA |
|---|---|---|
| JSON válido | 94,7 % | **100 %** |
| Acierto promedio por campo | 75,9 % | **91,2 %** |
| Señales de estafa (`red_flags`) | 22,2 % | **89,5 %** |
| Horario (`schedule`) | 55,6 % | **84,2 %** |
| Tipo de contrato | 61,1 % | **78,9 %** |
| Error medio en `skill_match` (0-10) | 1,83 | **1,26** |
| Error medio en el puntaje final (0-10) | 2,56 | **1,16** |
| Misma decisión de avisar (puntaje ≥ 7) | 73,7 % | **78,9 %** |
| Segundos por oferta (MLX) | 12,1 | 10,7 |

Lo que se aprendió en el camino:
- **El primer intento divergió.** Con `learning_rate` 1e-4 y `scale` 20, la pérdida bajó hasta el paso 100 y luego
  saltó (0,57 → 1,23 → 3,81). El adaptador del paso 100, aunque su pérdida de validación se veía bien, ya generaba
  JSON roto (se comía las comillas de las claves): 0 % válido. Con 3e-5 y `scale` 10 la validación bajó en cada
  control (0,871 → 0,252 → 0,230 → 0,210) y el formato quedó perfecto. La pérdida promedio no basta: hay que
  evaluar la salida real.
- **El alumno es conservador.** De las 5 ofertas de prueba que el maestro avisaría (puntaje ≥ 7), el alumno avisa
  1; en las que el maestro descarta, coincide en todas. Como respaldo local no manda ruido, pero se pierde ofertas
  buenas. La causa principal es que subestima `skill_match` en las ofertas que más encajan.

**Ronda 2 (2026-10-24)**, con las 456 ofertas etiquetadas: 364 de entrenamiento, 46 de validación y 46 de prueba;
360 pasos, misma configuración.

| Métrica (46 ofertas de prueba, contra el maestro) | Base, sin LoRA | LoRA (MLX) |
|---|---|---|
| JSON válido | 91,3 % | **100 %** |
| Acierto promedio por campo | 72,2 % | **94,7 %** |
| Señales de estafa (`red_flags`) | 31,0 % | **89,1 %** |
| Error medio en `skill_match` (0-10) | 3,52 | **1,33** |
| Error medio en el puntaje final (0-10) | 3,19 | **0,98** |
| Misma decisión de avisar | 60,9 % | **87,0 %** |
| Segundos por oferta (MLX, Mac M4) | 13,8 | 6,6 |

Ya no es conservador: de las 10 ofertas de prueba que el maestro avisaría, el alumno avisa las 10 (en la ronda 1,
1 de 5). A cambio, avisa 6 de más entre las otras 36.

**Fuera de MLX (GGUF + Ollama):**

| Versión | Misma decisión | `red_flags` | Error de puntaje | Tamaño |
|---|---|---|---|---|
| f16 | 87,0 % | 80,4 % | 0,98 | 2,9 GB |
| Q8_0 | 87,0 % | 80,4 % | 0,98 | 1,5 GB |
| **Q5_K_M** | **87,0 %** | **82,6 %** | **0,96** | **1,0 GB** |
| Q4_K_M | 84,8 % | 82,6 % | 1,11 | 0,9 GB |
| Q5_K_M en el servidor (ARM, 2 núcleos, sin GPU) | 87,0 % | 80,4 % | 0,96 | 21 s por oferta |

La caída en `red_flags` (89 → 80 %) no viene de cuantizar: el GGUF en 16 bits ya la tiene. Se descartó una causa
por vez. El modelo fusionado en MLX da 89,1 % (no es la fusión). Un GGUF en bf16 da 80,4 % (no es pasar de bf16 a
f16). Sin `format: json` da lo mismo (no es la gramática de Ollama). Con el prompt armado por el tokenizador de
Hugging Face da igual, con los mismos tokens (no es la plantilla). Queda la diferencia de cálculo entre MLX y
llama.cpp: con los mismos pesos, 4 ofertas al límite se deciden distinto. No se corrige con configuración; se mide
siempre en el motor donde va a correr.

**En producción, en modo sombra (2026-10-25).** Con las primeras 47 ofertas reales, el alumno coincide con el
maestro en la decisión solo el 38 % de las veces (error de puntaje 3,0): avisaría 28 que el maestro no avisa. Casi
toda la diferencia está en `skill_match`. Ante puestos técnicos remotos con un stack parecido pero no igual (piden
React y el perfil es Python), el alumno pone 8 y el maestro, 3. Las señales de estafa coinciden en el 100 %. El set
de prueba tenía muchas ofertas fáciles (conductores, cocineros), que inflan el 87 %. Por eso el agente no lo deja
decidir solo: en los desacuerdos manda el maestro, y sus respuestas sobre exactamente estas ofertas difíciles se
suman al dataset para la ronda 3.

## Límites

- MLX solo corre en Apple Silicon; en Linux o Windows con NVIDIA, lo equivalente es PEFT/Unsloth con CUDA.
- El alumno aprende a imitar al maestro, errores incluidos: el techo de calidad es el maestro.
- Un set de prueba chico y con muchos casos fáciles sobreestima la calidad: hay que medir con datos reales antes
  de dejar que el modelo decida solo (modo sombra).
- Unos cientos de ejemplos alcanzan para el formato y los criterios de esta tarea, no para tareas abiertas.
- Una MacBook Air no tiene ventilador: en entrenamientos largos baja el rendimiento por temperatura.
- En equipos con antivirus o control de archivos, la primera vez que Python carga una librería grande
  (`anthropic`, `mlx`) puede tardar varios minutos sin usar CPU; las siguientes cargas son inmediatas.
