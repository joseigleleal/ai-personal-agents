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
| Salidas | Telegram (avisos; "sin citas" como mucho cada 30 min) · `data/cita.log` · capturas y HTML en `data/cita/ultima/` · última pantalla vista en `data/cita/ultima.png` (por Telegram si cambió) · memoria `data/cita/aprendizaje.json` |

## Flujo

```
index.html ──▶ provincia ──▶ oficina + trámite ──▶ "Presentación con Cl@ve"
    ──▶ pasarela Cl@ve ──▶ "DNIe / Certificado electrónico" (el agente entrega el .p12)
    ──▶ acEntrada: ¿"no hay citas disponibles"?
          sí ─▶ aviso "⏳ sin citas" y termina
          no ─▶ 🚨 aviso ─▶ "Copiar a datos solicitante" + país ─▶ Solicitar cita
                ─▶ teléfono y correo ─▶ primer horario ─▶ (código SMS por Telegram) ─▶ Confirmar
                ─▶ ✅ aviso con fecha/hora y justificante ─▶ crea data/cita/RESERVADA y deja de buscar
```

## Decisiones de diseño (y por qué)

- **Modo fantasma (sin ventana).** Usa Chromium completo en modo *headless* con un user-agent normal: el
  cortafuegos del sitio ("Intrusion Prevention") rechaza el que dice `HeadlessChrome`, no el modo en sí. Así no
  se abre ninguna ventana que se cruce con lo que estás haciendo. Si algún día lo bloquean, `headless: false`
  vuelve a una ventana real ubicada fuera de la vista (`ventana_oculta: true`; macOS a veces la trae de vuelta).
  Para ver qué está viendo, `data/cita/ultima.png` se reemplaza en cada paso con la última pantalla.
- **Captura por Telegram solo si cambia.** Al final de cada revisión manda `ultima.png` si el resultado es
  distinto al de la última enviada (sin citas, hay citas, error en tal paso, bloqueo). Se descartó comparar la
  imagen o el texto: la misma página varía según el instante de la captura (el aviso "no hay citas" aparece
  un poco después de cargar), y eso reenviaba fotos iguales.
- **Certificado como archivo `.p12`.** Chrome solo elige un certificado de forma automática con políticas de
  empresa (MDM). Playwright entrega el certificado directamente (`client_certificates`) al servidor de Cl@ve
  que lo pide. El `.p12` que exporta macOS usa cifrado antiguo: se re-cifra con AES-256.
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
  intervalo 30 s (hasta 1 min); un error del sitio lo alarga ×1,5 (a veces se cae solo) y un bloqueo ×2.
  El intervalo al que hubo un bloqueo queda como **piso** (+25 %) durante 7 días; al caducar, vuelve a
  probar. Tras un bloqueo pausa `pausa_bloqueo_min` (15 por defecto), y si al volver lo bloquean otra vez,
  la siguiente pausa dura el doble (tope 4 h). Todo se ajusta en `ritmo:` de `config/cita.yaml`. Un
  candado evita dos revisiones a la vez.
- **Reintentos en dos niveles.** Cada botón tiene su tiempo de espera; si se agota, el agente reintenta ese
  paso con más tiempo (y si la página siguiente llegó tarde, sigue). Si el paso sigue fallando (o el sitio
  pierde la sesión al volver de Cl@ve), repite el recorrido completo con sesión nueva, hasta `reintentos`
  veces: con espera creciente si no había citas, **sin esperar** si ya las vio. Después de pulsar
  Confirmar no se repite nada, para no duplicar la reserva.
- **Memoria que aprende, sin modelo de lenguaje** (`aprendizaje.py` → `data/cita/aprendizaje.json`):
  - tiempo de espera por paso: ×1,5 tras agotarse (tope 90 s), −10 % con cada éxito (mínimo 30 s);
  - selector que funcionó en cada botón (hay alternativas por si el sitio cambia el `id`), se prueba primero;
  - ritmo entre revisiones, piso por bloqueos y pausa (ver "Ritmo aprendido");
  - historial de revisiones: horas en que hubo citas → en esa hora y la anterior revisa al doble de ritmo,
    sin bajar del mínimo ni del piso.
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
- **Avisos "sin citas" espaciados.** Con revisiones cada 1-2 min, avisar cada una sería ruido: el ⏳ sale
  como mucho cada `aviso_sin_citas_min` (30 por defecto; 0 = en cada revisión) e indica el ritmo actual.
- **Código SMS por Telegram.** Al reservar, el sitio manda un SMS al teléfono de `config/cita.yaml`. El agente
  avisa "📱 Te llegó un SMS" con sonido y espera hasta 8 min a que respondas **en el chat del bot del agente**
  solo con el código (4 a 8 dígitos). Lo lee con `getUpdates` (descarta los mensajes viejos), lo escribe y
  confirma. Conviene un bot propio (`TELEGRAM_BOT_TOKEN_CITA`) para que ningún otro proceso lea esos mensajes;
  hay que haberle enviado `/start` antes. Si no llega a tiempo, avisa y la cita no se confirma.
- **Sin spam de errores.** El mismo error se avisa como máximo una vez cada 3 h; las capturas de cada paso
  quedan en `data/cita/ultima/` para diagnosticar.

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
ssh -i ~/.ssh/oracle_agents ubuntu@<ip> tail -f ai-personal-agents/data/cita.log
ssh -i ~/.ssh/oracle_agents ubuntu@<ip> 'cd ai-personal-agents && git pull'   # actualizar el código
```

## Límites conocidos

- Las pantallas posteriores a "hay citas" se escribieron y simularon con los nombres de campos conocidos del
  sitio, pero solo se verifican de verdad cuando aparece una cita real: si algo falla, el aviso dice
  "entra tú ya" y el HTML queda en `data/cita/paginas/` para corregir.
- Para encontrar el límite, el agente tiene que acercarse a él: es esperable algún bloqueo de vez en cuando
  (cada 7 días, cuando caduca el piso). Si eso molesta, subir `ritmo.minimo_s`.
- Si el sitio cambia su HTML, hay que ajustar los selectores (las capturas muestran dónde se detuvo).
