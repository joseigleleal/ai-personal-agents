# Agente `cita` — cita previa de extranjería (toma de huellas)

**Qué hace:** revisa cada ~10 min si hay citas para la toma de huellas (TIE) en la web de cita previa
de extranjería, entrando con certificado digital por Cl@ve. Si hay, **reserva la primera** y avisa.

| | |
|---|---|
| Tipo | Automatización de navegador (Playwright), sin modelo de lenguaje |
| Frecuencia | launchd cada 8 min + espera aleatoria de 0–4 min (≈ 10 ± 2 min), las 24 h |
| Entradas | `config/cita.yaml` (provincia, trámite, contacto) · `data/cita/certificado.p12` · `CITA_CERT_PASSWORD` en `.env` |
| Salidas | Telegram (cada revisión) · `data/cita.log` · capturas en `data/cita/ultima/` |

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

- **Navegador real, fuera de la pantalla.** En modo *headless* un cortafuegos bloquea el acceso
  ("Intrusion Prevention"). Se usa una ventana normal ubicada fuera de la vista (`ventana_oculta: true`).
- **Certificado como archivo `.p12`.** Chrome solo elige un certificado de forma automática con políticas de
  empresa (MDM). Playwright entrega el certificado directamente (`client_certificates`) al servidor de Cl@ve
  que lo pide. El `.p12` que exporta macOS usa cifrado antiguo: se re-cifra con AES-256.
- **Cadena TLS validada por el agente.** El servidor de citas no envía su certificado intermedio y el
  navegador a veces lo rechaza. El agente descarga el intermedio, valida la cadena completa y el nombre del
  sitio contra las raíces de confianza, y **solo entonces** le indica al navegador que acepte esa clave.
- **Ritmo prudente.** Consultas seguidas provocan "sesión caducada" o bloqueos. Si detecta un bloqueo, pausa
  `pausa_bloqueo_min` (15 por defecto). Un candado evita dos revisiones a la vez.
- **Sin spam de errores.** El mismo error se avisa como máximo una vez cada 3 h; las capturas de cada paso
  quedan en `data/cita/ultima/` para diagnosticar.

## Uso

```bash
cp config/cita.example.yaml config/cita.yaml          # completar país, teléfono y correo
# exportar el certificado desde Acceso a Llaveros (.p12) → data/cita/certificado.p12
# .env: CITA_CERT_PASSWORD=...  (opcional: TELEGRAM_BOT_TOKEN_CITA para un bot propio)
.venv/bin/python -m agents.cita.run --ya --solo-revisar   # prueba sin reservar
./scripts/instalar_launchd.sh cita                         # programarlo
.venv/bin/python -m agents.cita.run --reiniciar            # buscar otra cita tras reservar
./scripts/instalar_launchd.sh cita desinstalar             # quitarlo
```

## Límites conocidos

- Las pantallas posteriores a "hay citas" se escribieron con los nombres de campos conocidos del sitio, pero
  solo se verifican cuando aparece una cita real: si algo falla, el aviso dice "entra tú ya".
- Si el sitio cambia su HTML, hay que ajustar los selectores (las capturas muestran dónde se detuvo).
