# Notas de desarrollo — Starlink Panel

Historial técnico de cambios: qué se tocó, por qué y en qué archivos.
Lo anterior a esta fecha está en el historial de git.

## TKT-0838 — Alerta de servicio caído con el dish conectado + cola de Telegram (2026-10-26)

**Problema.** Si Starlink suspende la cuenta (por ejemplo, por un pago rechazado), el dish sigue
enganchado al satélite y reporta `state = CONNECTED`. La alerta de desconexión no se dispara y
en el historial solo se ve el tráfico de bajada plano en ~15 kbps. Caso real: 14 h 29 min sin
servicio sin ningún aviso.

**Señales que se usan.**
- `disablement_code` del status crudo del dish (`starlink_grpc.get_status()`, no viene en
  `status_data()`). Normal: `OKAY`. Valores posibles: `NO_ACTIVE_ACCOUNT`, `ACCOUNT_DISABLED`,
  `DATA_OVERAGE_SANDBOX_POLICY`, `TOO_FAR_FROM_SERVICE_ADDRESS`, etc. (enum
  `UserTerminalDisablementCode`).
- Respaldo, por si el código no cambiara: prueba de salida a internet por TCP a
  `internet_check_hosts` (alcanza con que responda uno). Solo cuenta como problema de servicio si
  el dish está `CONNECTED`; con el dish desconectado ya avisa la alerta de desconexión.

**Cambios en `starlink_panel.py`.**
- `ServiceMonitor`: hilo propio que lee el código cada `service_check_interval_s` y prueba internet
  cada `internet_check_interval_s`, para no demorar el poll de 2,5 s.
- `service_problem()`: devuelve el motivo en castellano (`DISABLEMENT_LABELS`) o `None`.
- `AlertManager.process_service()`: avisa si el problema dura más de `service_alert_after_s` y
  vuelve a avisar al resolverse, con duración y horas de inicio y fin.
- `TelegramQueue`: sin internet no hay Telegram, así que el aviso de inicio de una suspensión casi
  nunca sale en el momento. `send_telegram()` ahora devuelve si pudo enviar; lo que falla queda en
  cola (tope `telegram_queue_max`, descarta lo más viejo) y `DishPoller` reintenta cada 30 s. Se
  marca «Aviso demorado (de las HH:MM)» solo si llega con más de 2 min de atraso. Todas las alertas
  existentes pasan ahora por la cola.
- `/api/status` agrega el bloque `service` (`disablement_code`, `internet_ok`, `problem`).

**Frontend.** Tarjeta «Servicio» (`static/index.html`, `updateService()` en `static/app.js`, clase
`.svc-detail` en `static/style.css`): Activo / Suspendido (con el motivo) / Sin internet.

**Configuración** (`alerts` en `config.json`, con valores por defecto en `DEFAULT_CONFIG` y en
`config.example.json`): `service_alert_enabled`, `service_alert_after_s` (300),
`service_check_interval_s` (30), `internet_check_interval_s` (60), `internet_check_hosts`,
`internet_check_timeout_s` (5), `telegram_queue_max` (50).

**Pruebas.** 7 casos con `send_telegram` interceptado (sin mensajes reales): reproducción de la
suspensión (inicio en cola, llega al volver junto con «restablecido»), corte de menos de 5 min sin
aviso, dish desconectado no duplica el aviso, sin internet con código `OKAY`, código sin traducir,
tope y orden de la cola, alerta de desconexión encolada sin internet. Además, corrida en loli contra
el dish real en un puerto de prueba: `disablement_code = OKAY`, `internet_ok = true`.

## TKT-0839 — Apuntado ideal vs real + registro de microcortes (2026-10-26)

**Apuntado.** El status crudo trae `alignment_stats`: dirección real (`boresight_*`) y la que el
dish calcula como ideal (`desired_boresight_*`, lo mismo que muestra la app en «Orientación»),
además de `tilt_angle_deg` y `attitude_uncertainty_deg`. `ServiceMonitor` ya pedía ese status cada
30 s, así que lo guarda junto con `alerts.lower_signal_than_predicted`.
- `alignment_advice()`: desvío angular real (fórmula de distancia esférica: a elevaciones altas,
  los grados de azimut pesan poco) y la indicación para corregirlo a mano. El giro se dice como
  izquierda o derecha «parado detrás del dish» y no con puntos cardinales, porque un desvío chico
  puede quedar dentro del mismo sector (190° y 180° son los dos «S»). Bajar el azimut = izquierda.
  Si `uncertainty_deg` supera `alignment.max_uncertainty_deg`, se avisa que el dato no es fiable.
- `/api/status` agrega `alignment`. Frontend: tarjeta «Apuntado» y, en el mapa del cielo, la
  dirección ideal como anillo blanco unido a la real con una línea punteada.

**Microcortes.** `get_history().outages` trae cada corte con inicio (ns en tiempo GPS), duración,
causa (`DishOutage.Cause`) y si cambió de satélite. El dish guarda ~12 h.
- `OutageStore`: hilo que los copia cada `outages.poll_interval_s` a la tabla `outages` de
  `history.db` (`start_ns` como clave primaria → `INSERT OR IGNORE`, releer no duplica). Unix =
  ns/1e9 + 315964800 − 18 (`GPS_EPOCH_UNIX`, `GPS_UTC_LEAP_S`). Retención = `history.retention_days`.
- `/api/outages?from&to`: filas, resumen por causa (`OUTAGE_CAUSE_LABELS` en castellano), total y
  estado de la última lectura. Frontend: sección «Microcortes», mismo rango que el historial.

**Config nueva:** `alignment.tolerance_deg` (3), `alignment.max_uncertainty_deg` (3),
`outages.poll_interval_s` (600).

**Pruebas.** Apuntado: valores reales del día (desvío 10,8°, «girarlo 12° a la izquierda: de 192°
(SSO) a 180° (S)», «inclinarlo 10° más»), bien apuntado, cruce de 0°/360°, levantar, incertidumbre
alta. `OutageStore` contra una base temporal: sin duplicados al releer, conversión GPS→Unix,
resumen por causa, retención. En loli contra el dish real: 8 microcortes guardados (7 «sin
respuesta del servidor», 1 «sin bajada», 6 s en total).

**Anotado en el README como pendiente:** `signal_quality`, reinicio programado por actualización,
restricción de velocidad del plan, configuración del dish (se puede leer; corrige lo que decía el
README sobre el modo nieve), `bootcount`, inclinación, tiempos de arranque, router y modo puente,
velocidad ethernet, clase de servicio, `is_snr_persistently_low`.
