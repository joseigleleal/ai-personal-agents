# Uso responsable

Estos agentes entran a sitios web, leen correo, usan modelos de lenguaje y pueden actuar por su cuenta (por
ejemplo, reservar una cita). Se comparten como ejemplo para replicar; antes de usarlos o adaptarlos, lee estas
reglas.

## Para qué es

- Automatizar tareas **propias**, con **tus propias** cuentas y datos: buscar empleo o proyectos, vigilar una
  tasa, conseguir **tu** cita.
- Aprender a construir agentes con un ejemplo real y pequeño.

## Qué no se debe hacer

- **Acaparar o revender citas** u otros turnos públicos, o reservar a nombre de otra persona sin su encargo.
  `cita` está pensado para una persona y su propio trámite.
- **Saltarse protecciones de un sitio**: resolver captchas de forma automática, evadir bloqueos o límites de
  uso, o consultar con una frecuencia que perjudique el servicio. Los agentes aprenden un ritmo prudente; no lo
  fuerces.
- **Usar cuentas, certificados o datos de otra persona** sin su permiso expreso.
- **Postular o escribir en masa**: los agentes de empleo y freelance proponen; enviar es decisión de la persona,
  una por una.
- **Juntar datos personales de terceros** (ofertas, correos, perfiles) para otros fines o compartirlos.
- **Mezclar cuentas o información de un empleador o cliente** con este proyecto personal.
- Incumplir los **términos de uso** de los sitios y API que se consultan, o las leyes de tu país.

## Seguridad mínima

- Secretos solo en `.env` y certificados solo en `data/`, ambos fuera de git. Activa el hook
  (`git config core.hooksPath scripts/hooks`) antes de publicar cambios.
- Los bots de Telegram solo atienden tu chat, y los botones solo ejecutan acciones de una lista cerrada.
- En un servidor: acceso solo por SSH con llave y secretos cifrados (`systemd-creds`), como en `scripts/oracle_*.sh`.
- Pon un tope de gasto si usas un modelo de pago (`MONTHLY_BUDGET_USD` y el límite de la consola del proveedor).

## Privacidad

- Lo que los agentes avisan pasa por Telegram, y lo que mandan a un modelo en la nube (Groq, Claude) pasa por
  ese proveedor. Para datos sensibles, usa Ollama (local).
- Logs, reportes y la base de datos quedan en `data/`, solo en tu máquina.

## Sin garantía

Se comparte tal cual, sin garantía de ningún tipo. Los sitios cambian y un agente puede fallar o equivocarse:
revisa lo importante. Cada persona es responsable de cómo lo usa y de cumplir las leyes y los términos de los
servicios que automatice.
