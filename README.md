# Starlink Panel

Dashboard web independiente, gratis y liviano para monitorear tu Starlink en tiempo real: estado de conexión, latencia, velocidad, obstrucción, y hacia dónde apunta físicamente el dish — incluso un mapa de qué satélites Starlink tenés sobre tu horizonte ahora mismo.

No es una app de SpaceX. Usa la API local de diagnóstico que el propio dish ya expone en tu red (`192.168.100.1:9200`), la misma que usa la app móvil oficial — sin nube, sin cuenta, sin telemetría a terceros.

![status](https://img.shields.io/badge/estado-v0.1-blue)

## Por qué existe

Inspirado en herramientas de la comunidad tipo Dishylink, pero pensado para correr en cualquier lado (no solo Windows) con dos comandos, y para poder compartirse con cualquier otro usuario de Starlink sin pedirles nada raro.

## Requisitos

- Python 3.9+
- Un dispositivo conectado a la **misma red que tu Starlink** (routeador en modo normal — si tu dish está en modo "bypass", corré esto en el equipo conectado directo al dish por ethernet)

## Instalación y uso

```bash
git clone https://github.com/camammoli/starlink-panel.git
cd starlink-panel
pip install -r requirements.txt
python3 starlink_panel.py
```

Abrí `http://localhost:8850` (o `http://<ip-de-esa-maquina>:8850` desde cualquier otro dispositivo de tu red).

No hace falta configurar la IP del dish — todos los Starlink usan la misma IP fija `192.168.100.1` en su red local, así que funciona out-of-the-box para cualquiera que lo corra.

### Opciones

```bash
python3 starlink_panel.py --port 8850 --dish 192.168.100.1:9200 --interval 2.5
```

## Qué muestra

- Estado de conexión, latencia, velocidad de bajada/subida, pérdida de paquetes
- % de obstrucción, si está obstruido ahora mismo, y duración/intervalo promedio de obstrucciones prolongadas
- Uptime, satélites GPS en uso, calidad de señal (SNR)
- Alertas activas del dish (recalentamiento, mástil desalineado, etc.)
- Gráfico de latencia/velocidad de los últimos minutos
- **Mapa del cielo**: hacia dónde apunta realmente el dish (dato real de telemetría) superpuesto con los satélites Starlink que están sobre tu horizonte en este momento (dato público de [Celestrak](https://celestrak.org), calculado con [satellite.js](https://github.com/shashwatak/satellite-js)) y de fondo el **mapa de obstrucciones real** (grilla de 123×123 de calidad de señal por dirección — misma data que arma el mapa de la app oficial de Starlink)
- **Historial persistente**: tabla + gráfico navegables por rango (última hora / 6h / 24h / 7 días / 30 días) y exportable a CSV
- **Alertas por Telegram** (opcionales): conexión caída/recuperada, obstrucción alta sostenida, alertas de hardware

## Configuración (opcional)

Copiá `config.example.json` a `config.json` y completá lo que quieras usar — **`config.json` nunca se versiona** (puede tener tu ubicación real y una credencial de Telegram):

```bash
cp config.example.json config.json
```

| Campo | Para qué sirve |
|---|---|
| `latitude` / `longitude` | Si las completás, el mapa del cielo arranca solo al abrir el panel, sin tener que activar la ubicación del navegador ni escribir coordenadas a mano. |
| `telegram_bot_token` / `telegram_chat_id` | Si completás ambos, se activan las alertas por Telegram (ver abajo). Sin esto, el panel funciona igual pero no manda nada. |
| `alerts.*` | Umbrales y tiempos sostenidos antes de avisar — pensado para no generar ruido (ver detalle abajo). |
| `history.resolution_s` | Cada cuántos segundos se guarda un punto agregado en el historial (default 60s = 1 punto por minuto). |
| `history.retention_days` | Cuántos días de historial se conservan antes de borrarse solo (default 90). |

### Alertas por Telegram

Pensadas para avisar solo lo que realmente importa, con tiempos sostenidos para evitar spam por blips momentáneos:

- 🔴 **Desconectado** / 🟢 **Reconectado** (con cuánto duró la caída) — recién después de `disconnect_after_s` segundos seguido sin `CONNECTED`, no en el primer poll.
- 🟡 **Obstrucción alta sostenida** — solo si `fraction_obstructed` supera `obstruction_threshold` durante `obstruction_sustained_s` segundos seguidos, no por un pico de un instante.
- ⚠️ **Alertas de hardware** (recalentamiento, mástil desalineado, motores trabados, agua detectada) — avisan una vez al activarse, no se repiten mientras siguen activas.

No hay alertas por latencia/velocidad puntual — generaría demasiado ruido para el valor que aporta.

## Arquitectura

- **Backend** (`starlink_panel.py`): un solo archivo Python, sin frameworks — `http.server` + `sqlite3` de la librería estándar, y [`starlink-grpc-core`](https://pypi.org/project/starlink-grpc-core/) (paquete de la comunidad que expone `starlink_grpc.py` del proyecto [sparky8512/starlink-grpc-tools](https://github.com/sparky8512/starlink-grpc-tools)) para hablarle al dish. Un hilo de fondo consulta el dish cada `--interval` segundos, actualiza el estado en memoria, alimenta el historial (SQLite, `history.db`, gitignored) y evalúa las alertas. El frontend solo hace polling HTTP normal a `/api/status`, `/api/history`, etc. — nunca gRPC directo.
- **Frontend** (`static/`): HTML/CSS/JS vanilla, sin build, sin dependencias propias (solo `satellite.js` vía CDN para el mapa del cielo, que se degrada solo si no hay internet — el resto del panel no depende de conexión a internet, solo de la red local).

## Nota sobre Celestrak

Celestrak actualiza los datos de satélites Starlink cada 2 horas y aplica una política de "una descarga por actualización" que parece ser **global por dataset, no por IP de cliente** (confirmado viendo el mismo timestamp de "última descarga" desde dos redes distintas). Por eso la descarga del TLE vive en el **backend** (`/api/tle`, cacheado en `tle_cache.json`, gitignored) y no en cada navegador — así todos los visitantes del panel comparten la misma descarga en vez de competir por la cuota de Celestrak entre sí. Si Celestrak no responde, se sirve el último dato cacheado en vez de fallar directo.

## Versión

**v0.1** — primera versión funcional, probada contra un dish Starlink Mini real (incluyendo historial, alertas, mapa de satélites y mapa de obstrucciones con datos reales).

## Pendiente / ideas para más adelante

- **Controles de escritura** (reiniciar el dish, guardarlo/"stow", resetear el mapa de obstrucciones, configurar horario de ahorro de energía) — la API local del dish los soporta, pero a propósito no están implementados todavía. Son acciones con impacto real (pueden cortar tu única conexión a internet un rato) y merecen una decisión aparte sobre cómo exponerlos con cuidado, no un botón suelto en el dashboard.
- **"Modo nieve"** (Off/Automático/Pre-calentar en la app oficial) — investigado y confirmado que **no** está expuesto por la API local del dish, solo existe como configuración de cuenta/nube. No hay forma de agregarlo a este panel.
- Solo probado contra un dish Starlink Mini real (el del autor) — la promesa de "cero configuración" para cualquier otro usuario depende de que todos los dishes usen la misma IP fija local, que es el comportamiento documentado y esperado, pero no verificado todavía con un segundo dispositivo.

## Deploy de referencia (uso personal del autor)

Corre como servicio systemd en un Raspberry Pi de la red local, junto con Home Assistant y otros servicios:

```bash
sudo systemctl status starlink-panel
```

Archivo de servicio en `deploy/starlink-panel.service` (ejemplo, ajustar rutas/usuario).

## License

MIT — see [LICENSE](LICENSE)
