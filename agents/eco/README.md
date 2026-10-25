# Agente `eco` — sugiere cómo responderías tú

**Qué hace:** le pasas un mensaje que te mandaron y te sugiere la respuesta que tú escribirías, con tu forma de
escribir (largo, muletillas, risas, emojis, varios mensajes cortos seguidos…), aprendida de tus propios chats.
**Nunca envía nada:** solo sugiere y tú decides si la copias. Todo corre en tu máquina; tus chats no salen de ahí.

| | |
|---|---|
| Tipo | Recuperación de ejemplos (embeddings) + perfil de estilo medido en código + modelo local que redacta |
| Frecuencia | A pedido (no se programa): por consola o desde un bot de Telegram que lo llame |
| Entradas | Chats exportados como pares `{"chat", "fecha", "contexto", "respuesta"}` (`ECO_DATOS`) |
| Salidas | La sugerencia (texto) · `data/eco.nosync/`: turnos, índice, perfil y dataset para LoRA |

## Flujo

```
chats exportados ──▶ turnos (tus mensajes seguidos = una respuesta) ──▶ perfil de estilo (conteos)
                 └─▶ embeddings del mensaje previo de cada turno (índice)
mensaje nuevo ──▶ 8 turnos con el mensaje previo más parecido ──▶ conversación de ejemplo + perfil ──▶ modelo local
              ──▶ «yo respondería: …»
```

## Cómo funciona por dentro

### En palabras simples

Para imitar cómo escribe alguien no basta con decirle a un modelo «escribe informal». Hay que mostrarle cómo
responde esa persona a mensajes parecidos. eco guarda miles de respuestas tuyas reales. Cuando llega un mensaje nuevo,
busca las veces en que te escribieron algo parecido y le muestra al modelo qué contestaste. Además le pasa datos
medidos de tu estilo: si escribes con mayúscula o sin punto final, cómo te ríes y cuántos mensajes mandas seguidos.
El modelo responde con esos ejemplos delante, como si siguiera la conversación.

No sabe lo que tú sabes (dónde estás, qué planes tienes): eco imita la forma, no el contenido. Por eso solo sugiere.

### Detalle técnico (para replicarlo)

| Archivo | Qué hace | Funciones / claves |
|---|---|---|
| `datos.py` | Lee los pares exportados y los junta en turnos (mensajes tuyos seguidos, con menos de 10 min entre sí) | `pares`, `turnos`, `PAUSA_MAX` |
| `estilo.py` | Perfil medido en código: mensajes por turno, largo, mayúscula inicial, punto final, ¿¡, risas, emojis, palabras y respuestas cortas frecuentes | `perfil`, `describir` |
| `run.py` | Prepara el índice, sugiere, compara con respuestas reales y exporta el dataset de LoRA | `preparar`, `sugerir`, `probar`, `dataset` |

- **Embeddings:** `bge-m3` en Ollama (multilingüe, ~1,2 GB), normalizados; similitud coseno con numpy. Con unos
  5000 turnos no hace falta una base vectorial: es una multiplicación de matrices.
- **Ejemplos como conversación:** los 8 turnos más parecidos van como mensajes previos (usuario = lo que te
  escribieron, asistente = lo que respondiste). Así el modelo continúa el patrón en vez de describirlo. Darlos como
  lista en el prompt hacía que repitiera el mensaje recibido o copiara el formato de la lista.
- **Mismo chat primero:** si dices con quién hablas (`--chat`), los ejemplos de ese chat suben un poco: el trato
  cambia por persona.
- **Modelo:** `qwen3:8b` en Ollama (`ECO_MODEL`), temperatura 0,6, máximo 5 líneas.
- **`--probar N`:** toma turnos reales al azar, los saca de los ejemplos y compara lo que dijiste con lo que sugiere.
  Muestra los pares para revisarlos a ojo y el parecido de significado promedio (coseno de embeddings).
- **`--dataset`:** exporta `train/valid` en formato de chat para `mlx_lm`, el mismo pipeline de
  [experimentos/lora_mlx](../../experimentos/lora_mlx/README.md). Es la fase 2: afinar un modelo chico con tus
  turnos para que el estilo quede en los pesos y no dependa de los ejemplos.

### Adaptarlo a tu caso

- **Otra fuente de chats:** cualquier exportador que deje JSONL con `chat`, `fecha` (ISO), `contexto` y `respuesta`
  sirve (`ECO_DATOS`). En una Mac, [mac-remote-agent](https://github.com/joseigleleal/mac-remote-agent) lo saca de
  la app de WhatsApp: «chats», «exporta …», «excluye …».
- **Otro modelo:** `ECO_MODEL` (redacta) y `ECO_EMBED` (embeddings) en `.env`; cualquiera de Ollama.
- **Telegram:** mac-remote-agent tiene «eco: <mensaje>» (con `ECO_DIR` apuntando a este repo).

## Uso

```bash
ollama pull bge-m3 && ollama pull qwen3:8b
.venv/bin/python -m agents.eco.run --preparar                 # una vez y cada vez que exportes más chats
.venv/bin/python -m agents.eco.run --sugerir "¿vienes hoy?" --chat Ana
.venv/bin/python -m agents.eco.run --probar 10
```

## Decisiones de diseño (y por qué)

- **Solo local:** son conversaciones privadas tuyas y de otras personas; ni los chats ni los embeddings pasan por
  una API externa.
- **Solo chats uno a uno, sin los del trabajo:** el exportador los filtra antes. En un grupo tu forma de escribir
  cambia y mezcla a muchas personas. El trabajo no se mezcla con un proyecto personal.
- **Estilo medido, no adivinado:** contar en código cuánto escribes y cómo es exacto y gratis. El modelo recibe
  números y ejemplos, no adjetivos.
- **Ejemplos antes que entrenar:** con recuperación funciona desde el primer día y mejora al exportar más chats. El
  LoRA queda como fase 2, cuando haya un set de prueba para medir si mejora.

## Límites

- Imita la forma, no sabe tus planes ni tus datos: puede sugerir algo que no es cierto. Por eso solo sugiere.
- Con contexto de un solo mensaje, la sugerencia puede no seguir el hilo de la conversación.
- Solo texto: los audios no se usan todavía (se podrían transcribir en local con Whisper).
- El modelo de 8B necesita ~6 GB de RAM libres; en un equipo con menos, usar un modelo más chico en `ECO_MODEL`.
