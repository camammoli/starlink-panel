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
- % de obstrucción y si está obstruido ahora mismo
- Uptime, satélites GPS en uso, calidad de señal (SNR)
- Alertas activas del dish (recalentamiento, mástil desalineado, etc.)
- Gráfico de latencia/velocidad de los últimos minutos
- **Mapa del cielo**: hacia dónde apunta realmente el dish (dato real de telemetría) superpuesto con los satélites Starlink que están sobre tu horizonte en este momento (dato público de [Celestrak](https://celestrak.org), calculado en tu propio navegador con [satellite.js](https://github.com/shashwatak/satellite-js) — no se manda tu ubicación a ningún lado, todo el cálculo es client-side)

## Arquitectura

- **Backend** (`starlink_panel.py`): un solo archivo Python, sin frameworks — `http.server` de la librería estándar + [`starlink-grpc-core`](https://pypi.org/project/starlink-grpc-core/) (paquete de la comunidad que expone `starlink_grpc.py` del proyecto [sparky8512/starlink-grpc-tools](https://github.com/sparky8512/starlink-grpc-tools)) para hablarle al dish. Un hilo de fondo consulta el dish cada `--interval` segundos y guarda el último estado + un historial corto en memoria; el frontend solo hace polling HTTP normal a `/api/status`, nunca gRPC directo.
- **Frontend** (`static/`): HTML/CSS/JS vanilla, sin build, sin dependencias propias (solo `satellite.js` vía CDN para el mapa del cielo, que se degrada solo si no hay internet — el resto del panel no depende de conexión a internet, solo de la red local).

## Nota sobre Celestrak

Celestrak actualiza los datos de satélites Starlink cada 2 horas y aplica una política de "una descarga por actualización" por IP. El panel cachea el TLE descargado en `localStorage` del navegador y no vuelve a pedirlo hasta que pasen 2 horas — si activás la ubicación varias veces seguidas es normal y esperado que reutilice el mismo dato.

## Versión

**v0.1** — primera versión funcional, probada contra un dish Starlink Mini real.

## Deploy de referencia (uso personal del autor)

Corre como servicio systemd en un Raspberry Pi de la red local, junto con Home Assistant y otros servicios:

```bash
sudo systemctl status starlink-panel
```

Archivo de servicio en `deploy/starlink-panel.service` (ejemplo, ajustar rutas/usuario).
