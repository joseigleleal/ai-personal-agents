# Agente `cita` — cita previa de extranjería (toma de huellas)

**Qué hace:** revisa cada pocos minutos (el ritmo lo aprende: de 1 a 30 min) si hay citas para la toma de huellas (TIE) en la web de cita previa
de extranjería, entrando con certificado digital por Cl@ve. Si hay, **reserva la primera** y avisa.
Si un paso falla, reintenta; y aprende de cada revisión (qué pasos tardan, qué botón funcionó, a qué
horas aparecen citas, qué tan seguido puede consultar sin que lo bloqueen) para ajustar esperas y ritmo.

| | |
|---|---|
| Tipo | Automatización de navegador (Playwright), sin modelo de lenguaje |
| Frecuencia | launchd lo lanza cada 1 min; revisa según el ritmo aprendido (1–30 min, empieza en 10), las 24 h |
| Entradas | `config/cita.yaml` (provincia, trámite, contacto) · `data/cita/certificado.p12` · `CITA_CERT_PASSWORD` en `.env` |
| Salidas | Telegram (un mensaje de estado que se edita solo; suena solo si hay citas, piden algo o hay un bloqueo o error nuevo; resumen diario; responde comandos) · `data/cita.log` · capturas y HTML en `data/cita/ultima/` · última pantalla vista en `data/cita/ultima.png` (por Telegram si cambió) · historial de capturas por revisión en `data/cita/historial/` (14 días) · memoria `data/cita/aprendizaje.json` |

## Flujo

```
index.html ──▶ provincia ──▶ oficina + trámite ──▶ "Presentación con Cl@ve"
    ──▶ pasarela Cl@ve ──▶ "DNIe / Certificado electrónico" (el agente entrega el .p12)
    ──▶ acEntrada (suele llegar ya con "no hay citas"): "Copiar a datos solicitante" + país ─▶ Aceptar
          ¿"no hay citas disponibles"? ─▶ estado "sin citas" y termina
          ¿página de error (502, 500…), sesión caducada o desconocida? ─▶ error: repite el recorrido
          menú "Solicitar Cita / Consultar / Anular…" (sale con o sin citas) ─▶ Solicitar Cita
                ¿"no hay citas disponibles"? ─▶ estado "sin citas" y termina
                si no ─▶ 🚨 aviso con la captura
                ─▶ teléfono y correo (un campo vacío que no conoce: te lo pregunta por Telegram)
                ─▶ lista de citas (1 a 3) + captcha: elige la primera (la más próxima) y te manda el captcha por Telegram
                ─▶ resumen con casillas "conforme" y "recibir por correo" ─▶ Confirmar
                ─▶ ✅ aviso con fecha/hora y justificante ─▶ crea data/cita/RESERVADA y deja de buscar
```

## Cómo funciona por dentro

### En palabras simples

Conseguir una cita de este tipo es como esperar que se libere una mesa en un restaurante lleno: alguien
cancela, la mesa queda libre unos segundos y gana quien llega primero. El agente hace de "persona en la
puerta": cada minuto se despierta, decide si ya le toca mirar y, si le toca, abre un navegador invisible,
recorre el sitio igual que una persona (provincia, trámite, identificación con certificado) y mira si hay citas.

Si hay, avisa con una foto de la pantalla y reserva la primera que vea; si el formulario pide un dato que no
tiene, o el captcha (las letras de la imagen), te lo pregunta por Telegram. Solo decide el ritmo (más seguido mientras todo va bien,
sin pasar del límite que aprendió), las esperas de cada botón y cuándo reintentar. Lo dudoso te lo deja a ti:
si una página parece un bloqueo o un error raro, te pregunta con dos botones y aprende de tu respuesta.

### Detalle técnico (para replicarlo)

**Archivos y piezas**

| Archivo | Qué hace | Funciones/clases clave |
|---|---|---|
| `agents/cita/run.py` | Arranque, recorrido del sitio con Playwright, reserva, avisos y comandos de Telegram | `main`, `recorrido`, `Flujo` (`entrar`, `hay_citas`, `reservar`, `pulsar`, `completar_faltantes`), `lanzar`, `leer_respuestas`, `atender_comando` |
| `agents/cita/aprendizaje.py` | Memoria que aprende (sin modelo de lenguaje) y la guarda en JSON | `Aprendizaje` (`timeout`, `orden`, `revision`, `piso`, `programar_siguiente`, `error_repetido`, `respuesta_bloqueo`) |
| `agents/cita/fijar_ip.cjs` | Se precarga en el Node de Playwright (`NODE_OPTIONS=--require`) y fija la IP del sitio | reemplaza `dns.lookup` y `dns.promises.lookup` |
| `core/notify.py` | Envíos a Telegram con un reintento a los 3 s; aviso de macOS como respaldo | `telegram`, `telegram_foto` (con botones), `telegram_album`, `macos` |
| `core/config.py` | Lee `.env` y `config/cita.yaml` (o el `.example` si no existe) | `env`, `load_config` |
| `scripts/oracle_cita.sh` | Instala el agente en una VM Linux con timer de systemd y claves cifradas | — |
| `tests/test_cita.py` | Memoria y reserva completa contra un sitio simulado (`contexto.route`) | `PAGINAS`, `servir` |

**Una corrida, paso a paso**

1. launchd (o el timer de systemd) lanza `python -m agents.cita.run` cada 1 min. Si existe `data/cita/RESERVADA`, sale.
2. `revisar_latido`: si pasaron más de 30 min sin arrancar, avisa. Si no hay otra revisión en curso,
   `leer_respuestas` llama a `getUpdates` (con el `tg_offset` guardado): atiende comandos de tu chat y los
   `callback_query` de los botones (`bloq:si|no:<hora>`, `err:si|no:<hora>`), responde con `answerCallbackQuery`
   y edita el mensaje con `editMessageCaption`; registra el menú con `setMyCommands` si cambió.
   `reportar_no_enviados` resume los avisos que no llegaron.
3. Si no toca (`Aprendizaje.toca`, salvo `--ya`), hay pausa vigente (`pausa_hasta`) o un candado `corriendo` de
   menos de 20 min, sale. Si sigue, crea el candado y reserva la próxima revisión a +20 min mientras corre.
4. `ip_alcanzable`: prueba conexión al puerto 443; si la IP del DNS local no responde, consulta DNS sobre HTTPS
   (Google y Cloudflare) y `fijar_ip` la fija. Si el sitio no responde, registra error y avisa (sin navegador).
5. `lanzar`: `clave_verificada` valida con `cryptography` la cadena TLS (descarga el intermedio indicado en el
   propio certificado y verifica contra `certifi`) y pasa su huella a Chromium con
   `--ignore-certificate-errors-spki-list`. Abre Chromium (`headless` con user-agent normal) con el `.p12` en
   `client_certificates` para los `certificado.origenes` de la config.
6. `recorrido` → `Flujo.entrar`: inicio → provincia → oficina y trámite → botón Cl@ve → "Certificado electrónico"
   → espera hasta 120 s la vuelta a `icpplus/ac…` (sesión caducada → `SesionCaducada`).
7. `Flujo.hay_citas`: rellena el formulario (`datos_solicitante`: Copiar + país, que Copiar no trae) y pulsa
   Aceptar, aunque la entrada ya diga "no hay citas" (pulsar Aceptar consulta de nuevo). Si sale el menú
   (`menu_citas`), pulsa "Solicitar Cita": solo hay citas si la respuesta no dice "no hay citas"; error del sitio →
   `ErrorDelSitio`, sesión caducada → `SesionCaducada`, otra página → `Inesperado`. Con citas: 🚨 con captura
   (`avisar_con_foto`, en un hilo aparte) y sigue.
8. `Flujo.reservar` (salvo `--solo-revisar`): oficina (si la pide) → contacto → lista de citas: marca
   la primera y, en la misma pantalla, el captcha (`pedir_captcha`, hasta `espera_captcha_min`) → marca todas
   las casillas visibles («Estoy conforme…», «Acepto recibir información… en el correo») → Confirmar. Antes de cada envío, `completar_faltantes` busca
   campos vacíos y pregunta por Telegram los que no conoce. Si ve "confirmada", crea `RESERVADA` y avisa ✅.
9. Si algo falla: `revisar_bloqueo` (frases de `BLOCKED`) lanza `Bloqueado` → pausa y aviso ⛔. Otro error →
   `recorrido` repite todo con cookies limpias hasta `reintentos`, salvo que ya se haya pulsado Confirmar o
   que el tipo de error pida pausa (`error_repetido`). El último error se avisa en lenguaje claro
   (`explicar_error`), como máximo una vez cada 3 h por mensaje (`error_nuevo`).
10. Al final: `archivar` guarda capturas en el historial, `enviar_vista_si_cambio` manda `ultima.png` con
    `sendPhoto` (con botones si es bloqueo o error nuevo), `programar_siguiente` fija la próxima revisión y se
    borra el candado.

**Estado y datos** (todo en `data/`, fuera de git)

- `cita/aprendizaje.json`: memoria. Claves principales: `pasos` (por paso: `ok`, `fallos`, `timeout_ms`,
  `ultimo_error`), `selectores` (el que funcionó por paso), `revisiones` (`t`, `r` = `sin_citas|hay_citas|reservada|error|bloqueo|falsa_alarma`,
  `cada_s`, `paso`, `causa`; últimas 3000), `ritmo` (`intervalo_s`, `bloqueos` con `confirmado`, `piso_desde`,
  `pausa_min`, `pausa_por_frase`, `falsas_alarmas`, `tipos_error`, `errores_pausa`, `racha`), `proxima`,
  `ultimo_aviso`, `tg_offset`, `menu`. Se escribe de forma atómica (archivo `.tmp` + `os.replace`).
- `cita/pausa_hasta`, `cita/corriendo`, `cita/RESERVADA`, `cita/latido`, `cita/ultimo_error`,
  `cita/ultima_enviada`: archivos de control (texto o solo su fecha de modificación).
- `cita/datos_extra.json`: campos del formulario que respondiste (`selector → {etiqueta, valor}`).
- `cita/no_enviados.json`: avisos que Telegram no entregó (hora, qué, motivo; máx. 20).
- Capturas: `cita/ultima/` y `cita/ultima.png` (se sobrescriben), `cita/bloqueos/` (50), `cita/paginas/`
  (corridas con citas), `cita/historial/<día>/` (`historial_dias`). Registro: `cita.log`.

**Reglas y cálculos que deciden** (el porqué está en "Decisiones de diseño")

- Ritmo (`ritmo:` en la config; valores por defecto en `RITMO`): empieza en `inicial_s` 600, cada revisión
  completa resta `baja_s` 30, entre `minimo_s` 60 y `maximo_s` 1800. Error o bloqueo sin confirmar: sin cambio.
- Piso: `cada_s` del último bloqueo confirmado × `sube_bloqueo` (1,2), menos `baja_piso_s` (12) por cada
  `horas_baja_piso` (2) sin bloqueos; nunca bajo `minimo_s`; vale `dias_piso` (7) días.
- Próxima revisión: intervalo ±20 % al azar; en horas intensas (horas con citas en 60 días y la anterior, más
  `horas_intensas`), la mitad, sin bajar del mínimo ni del piso.
- Pausas: bloqueo `pausa_bloqueo_min` (5), ×2 si se repite al volver (tope 240 min), −25 % la próxima vez por
  frase; error repetido 3 veces (`SEGUIDOS_PAUSA`) → `pausa_error_min` (5), recordado 7 días.
- Espera por paso: 30 s base, ×1,5 si se agota (tope 90 s), −10 % por éxito. `pulsar` reintenta una vez el paso.
- Recorrido: `reintentos` (2) repeticiones; espera `espera_reintento_s` (15) × 2^intento, 0 con citas a la vista.
- Error del sitio en un botón (502, 500, «requested url was rejected»): `pulsar` vuelve a la pantalla anterior
  (atrás del navegador; si el sitio no lo deja, abre de nuevo su dirección con la misma sesión),
  la rellena de nuevo (`preparar`) y pulsa otra vez, sin rehacer todo el recorrido. Empieza con `reintentos_paso`
  (2) y aprende por paso entre 0 y `reintentos_paso_max` (5): si entró justo con el último, suma uno; si se agotan
  3 veces seguidas, resta uno (en «tramite» el sitio no acepta el reenvío: llega a 0 y rehace el recorrido).
  Al volver espera el botón hasta `espera_volver_s` (20): antes aparece la verificación anti-robots del sitio. Con citas a la vista usa el tope y no espera. Nunca después de Confirmar.
- «requested url was rejected» ya no cuenta como bloqueo (salía muy seguido y nunca lo fue); bloqueo es «too many
  requests». Si el botón no aparece en `recargar_tras_s` (20) con la página cargada, la recarga una vez; si la vuelta
  de Cl@ve se traba `recargar_vuelta_clave_s` (40) en `procesarRespuestaClave`, también.
- Datos que faltan: `espera_dato_min` (4); captcha: `espera_captcha_min` (3; el sitio da 5 min para confirmar).
  Aviso ⏳: `aviso_sin_citas_min` (30).

**Para replicarlo**

- `pip install -r requirements.txt` (usa `playwright`, `requests`, `cryptography`, `pyyaml`, `python-dotenv`) y
  `python -m playwright install chromium` (en Linux, `--with-deps`).
- Un bot de Telegram (BotFather) al que le hayas enviado `/start`, y un certificado digital exportado como `.p12`.
- `.env`: `CITA_CERT_PASSWORD`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, u opcionalmente
  `TELEGRAM_BOT_TOKEN_CITA` y `TELEGRAM_CHAT_ID_CITA` para un bot propio.
- Copiar `config/cita.example.yaml` a `config/cita.yaml`, probar con `--ya --solo-revisar` y programarlo (ver "Uso").

### Adaptarlo a tu caso

- **Genérico (reutilizable tal cual):** `aprendizaje.py` completo (ritmo, piso, pausas, tiempos por paso,
  selector recordado); `Flujo.pulsar`, `foto`, `revisar_bloqueo` y `completar_faltantes`; el arranque de `main`
  (candado, pausa, `toca`, latido); `leer_respuestas`, `atender_comando`, `enviar_vista_si_cambio`, el historial
  y `no_enviados`; `core/notify.py`; el patrón de corrida única programada cada minuto.
- **Específico de este sitio:** `ICP_HOST`/`ICP`, los textos `NO_CITAS`, `BLOCKED`, `ERRORES_SITIO` y
  `SESION_CADUCADA`, los selectores (`#btnAceptar`, `#txtIdCitado`, `#btnConfirmar`…) y la vuelta de Cl@ve.
- **Para otro sitio de citas o turnos:** reescribir `Flujo.entrar`, `Flujo.hay_citas` y `Flujo.reservar` con
  sus pasos y selectores (cada clic con `pulsar("<paso>", "<selector>", "<alternativo>")`), ajustar las
  constantes de textos y `PASOS` (nombres claros para los avisos), y los campos de la config que lea.
- **Sin certificado:** si no existe el `.p12` (`certificado.archivo`), `lanzar` no pasa `client_certificates`; si
  el sitio envía bien su cadena TLS, se puede quitar la llamada a `clave_verificada` en `lanzar`. `ip_alcanzable`/`fijar_ip.cjs`
  solo hacen falta si el DNS local falla.
- **Otro trámite sin reserva:** `--solo-revisar` ya avisa sin reservar; basta con cambiar `hay_citas`.
- Actualizar `tests/test_cita.py` (`PAGINAS`) con el HTML del nuevo sitio para ensayar sin red.

## Decisiones de diseño (y por qué)

- **Modo fantasma (sin ventana).** Usa Chromium completo en modo *headless* con un user-agent normal: el
  cortafuegos del sitio ("Intrusion Prevention") rechaza el que dice `HeadlessChrome`, no el modo en sí. Así no
  se abre ninguna ventana que se cruce con lo que estás haciendo. Si algún día lo bloquean, `headless: false`
  vuelve a una ventana real ubicada fuera de la vista (`ventana_oculta: true`; macOS a veces la trae de vuelta).
  Para ver qué está viendo, `data/cita/ultima.png` se reemplaza en cada paso con la última pantalla.
- **"Hay citas" solo si lo ve de verdad.** Antes bastaba con no leer "no hay citas disponibles", y una
  página "502 Bad Gateway" del sitio pasaba por citas disponibles (5 falsas alarmas en una noche). Ahora
  exige ver el formulario de datos del solicitante sin ese aviso; una página de error del sitio repite el
  recorrido y una desconocida se reporta como error. El aviso 🚨 llega **con la captura** de esa pantalla,
  enviada en paralelo para no demorar la reserva.
- **Captura por Telegram solo si cambia.** Al final de cada revisión manda `ultima.png` si el resultado es
  distinto al de la última enviada (sin citas, hay citas, error en tal paso, bloqueo). Se descartó comparar la
  imagen o el texto: la misma página varía según el instante de la captura (el aviso "no hay citas" aparece
  un poco después de cargar), y eso reenviaba fotos iguales.
- **Certificado como archivo `.p12`.** Chrome solo elige un certificado de forma automática con políticas de
  empresa (MDM). Playwright entrega el certificado directamente (`client_certificates`) al servidor de Cl@ve
  que lo pide. El `.p12` que exporta macOS usa cifrado antiguo: hay que re-cifrarlo con AES-256 antes de usarlo
  (`openssl pkcs12 -legacy -in viejo.p12 -nodes | openssl pkcs12 -export -out data/cita/certificado.p12`).
- **Cadena TLS validada por el agente.** El servidor de citas no envía su certificado intermedio y el
  navegador a veces lo rechaza. El agente descarga el intermedio, valida la cadena completa y el nombre del
  sitio contra las raíces de confianza, y **solo entonces** le indica al navegador que acepte esa clave.
- **IP de respaldo si el DNS local falla.** El DNS de la red puede entregar una IP del sitio que deja de
  responder mientras la IP pública sigue funcionando: el navegador solo ve "tardó demasiado en responder".
  Antes de abrir el navegador, el agente prueba la conexión; si falla, pide la IP por DNS sobre HTTPS
  (Google/Cloudflare) y la fija en Chromium y en el proceso Node de Playwright (`fijar_ip.cjs`), porque con
  `client_certificates` las conexiones salen desde Node y este no respeta `--host-resolver-rules`. El
  certificado del sitio se sigue validando igual.
- **Ritmo aprendido.** Las citas que otros cancelan se liberan en cualquier momento y duran poco, así que
  conviene revisar seguido; pero consultar demasiado provoca bloqueos, y el sitio no publica su límite. El
  agente lo busca solo, como el control de congestión de TCP: cada revisión sin problemas acorta el
  intervalo 30 s (hasta 1 min). Un error o un bloqueo **no cambian el ritmo por sí solos**: el agente
  pregunta por Telegram y espera tu respuesta (detectar un bloqueo por el texto de la página a veces falla,
  y subir el ritmo por una falsa alarma lo deja lento sin motivo). Mientras tanto, tras un bloqueo detectado
  pausa `pausa_bloqueo_min` (5 por defecto: lo que bastaba al revisar a mano). Si confirmas que fue bloqueo,
  el intervalo pasa a `sube_bloqueo` × el que había (1,2 por defecto: subir poco, no duplicar) y ese valor
  queda como **piso aprendido**: no vuelve a bajar de ahí. Si lo bloquean otra vez estando en el piso, el piso
  sube otro escalón (1 → 1,2 → 1,44 → 1,73 min…), hasta dar con un ritmo que el sitio acepte. Sin piso, bajar
  a 1 min y pagar cada «429 Too Many Requests» con una pausa solo repetía el choque cada 20-30 min.
  El piso **baja suave**: cada `ritmo.horas_baja_piso` (2) sin bloqueos confirmados baja `ritmo.baja_piso_s`
  (12 s): 3,0 → 2,8 → 2,6 min… Así vuelve a probar el límite poco a poco; antes el piso duraba 7 días entero y
  al caducar el ritmo saltaba de golpe al mínimo, justo lo que había provocado los bloqueos. Con
  `horas_baja_piso: 0` no baja. Se olvida a los `ritmo.dias_piso` días (7); con 0 no hay piso (cada bloqueo es
  solo una pausa).
  - **Pausa por tipo** (la frase que lo delató, p. ej. «too many requests»): `pausa_bloqueo_min` (5 por
    defecto), el doble si al volver lo bloquean otra vez (tope 4 h); la que funcionó se recuerda para ese
    tipo y la próxima vez prueba un 25 % menos.
  - La captura del aviso ⛔ es la página del bloqueo, y el aviso dice qué frase lo detectó. Llega con dos
    botones, **"✅ Sí es bloqueo" / "❌ No es bloqueo"**: si respondes que sí, aplica lo de arriba; si
    respondes que no, levanta la pausa y recuerda esa frase en ese paso como falsa alarma. Lee
    la respuesta en el siguiente arranque (≤ 1 min); el mensaje se edita con lo que hizo.
  - Los **errores** (un "Internal Server Error", una página que no carga) también llegan con botones,
    **"🚫 Fue bloqueo" / "⚠️ Error normal"**: un sitio puede frenar en vez de bloquear. Si respondes que fue
    bloqueo, se aprende como bloqueo confirmado (piso ×1,2 y pausa).
    Si no respondes, el bloqueo o error queda sin contar (se ve en `--aprendizaje` como «sin confirmar»).
  - Las capturas normales se sobrescriben (`ultima.png`, `ultima/`); las de bloqueo y falsa alarma se
    conservan en `data/cita/bloqueos/` (captura + HTML, las 50 más recientes). Todo se ajusta en `ritmo:` de `config/cita.yaml`. Un
  candado evita dos revisiones a la vez.
- **Errores que se repiten: pausa en vez de reintentar.** Si un mismo tipo de error (paso + causa) sale
  3 veces seguidas, contando los reintentos, reintentar no lo arregla: el agente pausa `pausa_error_min`
  (5 por defecto) y luego sigue **al mismo ritmo**. Lo recuerda: la próxima vez que ese tipo aparezca, pausa
  al primer intento. Lo olvida si pasa 7 días sin salir. Con citas a la vista nunca pausa.
- **Reintentos en dos niveles.** Cada botón tiene su tiempo de espera; si se agota, el agente reintenta ese
  paso con más tiempo (y si la página siguiente llegó tarde, sigue). Si el paso sigue fallando (o el sitio
  pierde la sesión al volver de Cl@ve), repite el recorrido completo con sesión nueva, hasta `reintentos`
  veces: con espera creciente si no había citas, **sin esperar** si ya las vio. Después de pulsar
  Confirmar no se repite nada, para no duplicar la reserva.
- **Memoria que aprende, sin modelo de lenguaje** (`aprendizaje.py` → `data/cita/aprendizaje.json`):
  - tiempo de espera por paso: ×1,5 tras agotarse (tope 90 s), −10 % con cada éxito (mínimo 30 s);
  - selector que funcionó en cada botón (hay alternativas por si el sitio cambia el `id`), se prueba primero;
  - ritmo entre revisiones, pausa por bloqueo y por error repetido (ver "Ritmo aprendido");
  - historial de revisiones: horas en que hubo citas → en esa hora y la anterior revisa al doble de ritmo,
    sin bajar del mínimo (ni del piso, si está activo).
  Se descartó LangGraph/LangChain aquí: es un recorrido fijo y, cuando aparecen citas, duran segundos; un
  modelo en medio agrega demora e imprevisibilidad justo en el paso que no admite errores.
- **Ensayo de la reserva.** Las pantallas de la reserva solo se ven cuando hay citas. `tests/test_cita.py`
  las simula (mismos ids de campos) y recorre reserva completa, reintentos y botones alternativos con
  Chromium sin ventana. Cuando hay citas reales, el agente guarda capturas y HTML en `data/cita/paginas/`
  para comparar y ajustar la simulación.
- **Aviso de pausas largas.** launchd no corre con la Mac dormida (tapa cerrada, batería). Cada arranque deja
  un "latido"; si pasaron más de 30 min sin ninguno, avisa por Telegram al volver.
- **Ritmo decidido por el agente.** launchd lo lanza cada 1 min, pero solo revisa cuando toca según la
  memoria; así puede acelerar o frenar sin reinstalar nada.
- **Un mensaje de estado en vez de un ⏳ por revisión.** Con revisiones cada pocos minutos, un mensaje por cada una
  era ruido. Ahora el bot edita siempre el mismo mensaje (`notify.telegram_estado`, `editMessageText`): última
  revisión, conteo de la última hora, ciclo y próxima. Suena solo lo que pide atención (citas, captcha, dato que falta,
  bloqueo o error nuevo); volver a "sin citas" llega en silencio, y a las `resumen_diario_hora` (21) llega el
  resumen de 24 h sin sonido. Con `estado_editable: false` vuelve el ⏳ cada `aviso_sin_citas_min`.
- **Ciclo real, no solo la espera.** El ritmo aprendido es la espera *después* de cada revisión; el ciclo que se ve
  llegar suma lo que dura la revisión (~1,4 min) y la espera del timer. Los mensajes muestran el ciclo medido
  (`Aprendizaje.ciclo_real`, mediana entre inicios). No se acortó la espera para igualarlo: el piso se aprendió con
  ese ciclo y acortarlo traería bloqueos. En el servidor el timer mira cada 20 s para no sumar demora inútil.
- **"No hay citas" en la entrada no es la última palabra.** La entrada suele llegar con ese aviso sobre el
  formulario; antes el agente se rendía ahí, pero pulsar Copiar, elegir el país y Aceptar es una consulta nueva
  (así salió una cita en una prueba a mano). Se probó una tercera consulta en la misma sesión y no se puede: la
  respuesta sin citas no trae el formulario (su botón lleva al inicio), y ni «atrás» ni abrir de nuevo su
  dirección lo devuelven.
- **Un paso que no carga: se lee la página antes de culpar a la lentitud.** Varias veces «no respondió a tiempo»
  al abrir el inicio era en realidad un 429 «Too Many Requests» (bloqueo) y otras un 503 «Service Unavailable»
  (sitio caído). Mezclados, el agente preguntaba y una caída podía aprenderse como bloqueo y frenar el ritmo sin
  motivo. Ahora `leer_pagina_del_error` mira la página del error: 429 → bloqueo directo, 503 → error del sitio.
- **Hay citas solo si "Solicitar Cita" no responde "no hay citas".** Antes bastaba con no ver "no hay citas", y
  una página de "sesión caducada" pasó por citas disponibles (aviso 🚨 falso). Tampoco basta con ver el menú
  "Solicitar Cita / Consultar / Anular": sale aunque no haya citas, así que el agente lo pulsa y mira la respuesta.
- **Captcha por Telegram.** En la pantalla de la lista de citas («Selecciona una de las siguientes citas
  disponibles», con «dispones de 5 minutos») el sitio pide escribir las letras de una imagen; no manda SMS.
  El agente marca la primera cita (la más próxima), busca el campo (captcha, código, caracteres…), recorta la
  imagen y te la manda con sonido junto con el horario; espera `espera_captcha_min` a que respondas **en el chat
  del bot del agente** con el texto tal cual (letras y números, sin espacios; distingue minúsculas). Lo lee con `getUpdates` (descarta los mensajes viejos), lo escribe y
  confirma. No se guarda: cambia cada vez. Conviene un bot propio (`TELEGRAM_BOT_TOKEN_CITA`) para que ningún otro proceso lea esos mensajes;
  hay que haberle enviado `/start` antes. Si no llega a tiempo, avisa y la cita no se confirma.
- **Datos que faltan, por Telegram.** Antes de enviar cada formulario (datos del solicitante, oficina, contacto,
  confirmar) busca campos visibles que sigan vacíos: un dato que el sitio pide y `config/cita.yaml` no tiene.
  Manda "📝 Para la cita piden «…»" con la captura (en una lista, también las opciones numeradas) y espera
  `espera_dato_min` (4) tu respuesta: el valor, el número de la opción o «-» para dejarlo vacío. Lo guarda en
  `data/cita/datos_extra.json` y no lo vuelve a preguntar. Por qué no basta con mandar el enlace: el sitio no
  deja la misma persona con dos sesiones a la vez, así que entrar tú mientras el agente reserva no sirve.
  Observaciones y el captcha no se preguntan por esta vía.
- **Tipos de error que ya respondiste.** Cada error se clasifica por paso y causa (p. ej. «vuelta_clave |
  bad gateway», «acceso_clave | no respondió a tiempo»). Lo que respondas con los botones vale para ese
  tipo: si dijiste "error normal", los siguientes de ese tipo quedan en el log y el historial sin avisar ni
  preguntar; si dijiste "fue bloqueo", los siguientes se tratan como bloqueo confirmado sin preguntar. Si
  van 3 revisiones seguidas con error, avisa igual (algo más serio puede estar pasando).
- **Rechazo marcado como falsa alarma: repite ya.** Si dijiste que «requested url was rejected» en un paso
  no es bloqueo, el agente ya no espera el botón (hasta 90 s en una página sin salida): repite el recorrido.
- **Sonido solo en la Mac.** `afplay` no existe en Linux y en el servidor cortaba el recorrido justo después
  de ver citas; ahora se usa solo si está.
- **Historial de capturas.** Cada revisión guarda lo que vio en `data/cita/historial/<día>/<hora>_<resultado>/`:
  sin citas, solo la última pantalla (~200 KB); cualquier otro resultado, todos los pasos con su HTML (~3 MB).
  Se borra a los `historial_dias` (14): unos 2 GB como máximo. Se piden por Telegram con `/historial` y `/ver`.
- **Comandos por Telegram.** En el chat del bot del agente (solo atiende tu chat; botones de otros se ignoran):

  | Comando | Qué hace |
  |---|---|
  | `/estado` | última revisión, pausa, próxima revisión, resumen de 24 h |
  | `/informe [horas]` | resultados, % de revisiones completas, errores por tipo, citas vistas, bloqueos |
  | `/aprendido` | lo mismo que `--aprendizaje`: ritmo, piso, bloqueos, tipos de error |
  | `/captura` | la última pantalla vista |
  | `/historial [n]` · `/ver HH:MM [AAAA-MM-DD]` | revisiones guardadas (no "sin citas") y sus capturas en un álbum |
  | `/log [n]` | últimas líneas del registro |
  | `/revisar` · `/pausar [horas]` · `/seguir` | revisar ya, pausar, quitar la pausa |

  No hay un proceso escuchando: se leen en cada arranque del agente (cada minuto, salvo durante una
  revisión), así que responde en 1-2 min. Comandos fijos en vez de un modelo de lenguaje: respuestas
  exactas, sin costo y sin inventar datos. El menú de comandos de Telegram se registra solo.
- **Sin spam de errores.** El mismo error se avisa como máximo una vez cada 3 h, en lenguaje claro (en qué paso,
  qué pasó y si tienes que hacer algo; los detalles técnicos quedan en el log); las capturas de cada paso
  quedan en `data/cita/ultima/` para diagnosticar.
- **Avisos que no llegan.** Si Telegram falla (red, servidor caído, exceso de mensajes), `core/notify.py`
  reintenta una vez a los 3 s. Si tampoco sale, queda en el log con el motivo y en `data/cita/no_enviados.json`;
  en el siguiente arranque (cada 1 min) te llega un resumen en lenguaje claro (hora, qué aviso y por qué no llegó).
  Así se sabe si se perdió algo, sin reenviar avisos viejos uno por uno.

## Uso

```bash
cp config/cita.example.yaml config/cita.yaml          # completar país, teléfono y correo
# exportar el certificado desde Acceso a Llaveros (.p12) → data/cita/certificado.p12
# .env: CITA_CERT_PASSWORD=...  (opcional: TELEGRAM_BOT_TOKEN_CITA para un bot propio)
.venv/bin/python -m agents.cita.run --ya --solo-revisar   # prueba sin reservar
.venv/bin/python -m unittest tests.test_cita -v           # ensayo de la reserva con el sitio simulado
.venv/bin/python -m agents.cita.run --aprendizaje         # qué aprendió: fallos por paso, horas con citas, ritmo
./scripts/instalar_launchd.sh cita                         # programarlo
.venv/bin/python -m agents.cita.run --reiniciar            # buscar otra cita tras reservar
./scripts/instalar_launchd.sh cita desinstalar             # quitarlo
```

## En un servidor (Oracle Cloud, 24 h)

La Mac deja de revisar cuando duerme. `scripts/oracle_cita.sh ubuntu@<ip>` lleva el agente a una VM A1 de
Oracle (Always Free, ARM; la Micro de 1 GB no alcanza para Chromium) desde la Mac:

- **Claves cifradas.** `CITA_CERT_PASSWORD` y el bot de Telegram se cifran con `systemd-creds`; el servicio las
  descifra solo en memoria al correr. No hay `.env` en el servidor. Límite: la llave que las descifra vive en la
  misma VM (solo root la lee), así que protege de fugas (respaldos, copias), no de alguien que ya sea root.
- **Certificado** con permisos 600 en una carpeta 700; SSH solo con llave, sin root, `fail2ban` y
  actualizaciones de seguridad automáticas. Si la VM se viera comprometida: revocar el certificado en la FNMT.
- **Un solo lugar a la vez.** El sitio no tolera dos sesiones de la misma persona: con la Mac y la VM revisando
  a la vez, ambas reciben "sesión caducada" o errores 500. Por eso el script quita `cita` de launchd en la Mac;
  para volver: `./scripts/instalar_launchd.sh cita` y `sudo systemctl disable --now agentic-cita.timer` en la VM.
- Timer de systemd cada 1 min llamando a Python directo (no `run_agent.sh`, que actualiza el repo en cada arranque);
  el agente decide el ritmo. Modo fantasma: no necesita pantalla virtual (`xvfb`).

```bash
./scripts/oracle_cita.sh ubuntu@<ip>                                   # instalar y probar
```

**Comandos para revisarlo desde la Mac.** Con un alias en `~/.ssh/config` basta `ssh oracle`:

```
Host oracle
  HostName <ip>
  User ubuntu
  IdentityFile ~/.ssh/oracle_agents
```

```bash
ssh oracle                                                              # entrar a la VM (salir: exit)

# Agente
ssh oracle 'tail -20 ai-personal-agents/data/cita.log'                  # últimas revisiones
ssh oracle 'tail -f ai-personal-agents/data/cita.log'                   # en vivo (Ctrl+C)
ssh oracle 'cd ai-personal-agents && .venv/bin/python -m agents.cita.run --aprendizaje'  # ritmo, piso, bloqueos
ssh oracle 'systemctl list-timers agentic-cita.timer'                   # próximo arranque
ssh oracle 'journalctl -u agentic-cita.service -n 20 --no-pager'        # registro de systemd
scp oracle:ai-personal-agents/data/cita/ultima.png /tmp/ && open /tmp/ultima.png   # última pantalla

# Control
# revisar ya (la variable se quita apenas arranca: si no, el arranque que el timer deja en cola la hereda)
ssh oracle 'sudo systemctl set-environment CITA_ARGS=--ya && sudo systemctl start --no-block agentic-cita.service; sleep 3; sudo systemctl unset-environment CITA_ARGS'
ssh oracle 'sudo systemctl stop agentic-cita.timer'                     # pausar
ssh oracle 'sudo systemctl start agentic-cita.timer'                    # reanudar
ssh oracle 'cd ai-personal-agents && git pull'                          # actualizar el código tras un push

# Máquina y seguridad
ssh oracle 'uptime; free -h; df -h /'                                   # carga, memoria, disco
ssh oracle 'systemctl --failed'                                         # servicios con fallas
ssh oracle 'sudo fail2ban-client status sshd'                           # IPs bloqueadas por intentar entrar
ssh oracle 'last -n 10'                                                 # últimos accesos: deberían ser solo tuyos
```

## Límites conocidos

- Las pantallas posteriores a "hay citas" se escribieron y simularon con los nombres de campos conocidos del
  sitio, pero solo se verifican de verdad cuando aparece una cita real: si algo falla, el aviso dice
  "entra tú ya" y el HTML queda en `data/cita/paginas/` para corregir.
- Para encontrar el límite, el agente tiene que chocar con él: hasta que el piso llega al ritmo que el sitio
  acepta hay varios bloqueos (cada uno cuesta una pausa), y al bajar el piso vuelve a probar. Si eso
  molesta, subir `ritmo.sube_bloqueo` (escalones más grandes), `ritmo.horas_baja_piso` o `ritmo.minimo_s`.
- Si el sitio cambia su HTML, hay que ajustar los selectores (las capturas muestran dónde se detuvo).
- El campo y la imagen del captcha se buscan por nombres probables (captcha, código, caracteres…): la pantalla
  se vio en una captura del celular, pero sus ids reales solo se confirman cuando el agente llegue ahí. Si no los encuentra, el campo queda vacío y el aviso de error lo dice.
- No adelanta una cita ya reservada: el sitio no deja mover una cita (solo anular y pedir otra) ni tener dos
  citas del mismo trámite con el mismo NIE, y anular primero para buscar otra arriesga quedarse sin ninguna.
  Tras reservar, el agente deja de buscar.
