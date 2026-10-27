#!/usr/bin/env python3
"""Starlink Panel — dashboard independiente de estado de tu Starlink.

Corre en cualquier dispositivo conectado a la MISMA RED que tu Starlink: el
dish siempre usa la IP fija 192.168.100.1:9200 para su API local de
diagnostico, asi que no hace falta configurar nada especifico de tu red.

Instalacion:
    pip install -r requirements.txt

Uso:
    python3 starlink_panel.py [--port 8850] [--dish 192.168.100.1:9200]

Despues abri http://<esta-maquina>:<puerto> desde cualquier navegador de tu
red local.

Configuracion opcional en config.json (ver config.example.json) — ubicacion
para el mapa de satelites, umbrales de alertas, retencion de historial, y
credenciales de un bot de Telegram si queres avisos de conexion/obstruccion.
config.json NUNCA se versiona (contiene tu ubicacion real y, si la usas, una
credencial) — ver .gitignore.
"""
import argparse
import copy
import csv
import io
import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import starlink_grpc

BASE_DIR = Path(__file__).parent
STATIC_DIR = (BASE_DIR / "static").resolve()
HISTORY_MAXLEN = 240  # buffer en memoria (grafico "ultimos minutos"), a intervalo default de 2.5s

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle"
TLE_CACHE_FILE = BASE_DIR / "tle_cache.json"
TLE_MAX_AGE_S = 2 * 60 * 60  # Celestrak actualiza cada 2h y aplica 1 descarga por ventana

DB_FILE = BASE_DIR / "history.db"

# ── Configuracion ────────────────────────────────────────────────────────────
CONFIG_FILE = BASE_DIR / "config.json"
DEFAULT_CONFIG = {
    "latitude": None,
    "longitude": None,
    "telegram_bot_token": None,
    "telegram_chat_id": None,
    "alerts": {
        "enabled": True,  # solo manda algo si ademas hay token+chat_id
        "disconnect_after_s": 120,       # cuanto tiempo desconectado antes de avisar
        "reconnect_notify": True,
        "obstruction_threshold": 0.15,   # fraccion (0.15 = 15%)
        "obstruction_sustained_s": 300,  # cuanto tiempo sostenido antes de avisar
        "hardware_alerts": True,         # thermal/mastil/agua/motor - siempre avisan si estan on
        "weather_alert_enabled": True,
        "weather_alert_obstruction_threshold": 0.10,  # cruzar esto dispara el chequeo de clima
        "weather_alert_forecast_hours": 2,  # chequea la hora actual + estas siguientes
        # Servicio caido aunque el dish siga "CONNECTED" al satelite: cuenta
        # suspendida (pago rechazado), plan sin datos, o sin salida a internet.
        "service_alert_enabled": True,
        "service_alert_after_s": 300,     # cuanto tiempo sostenido antes de avisar
        "service_check_interval_s": 30,   # cada cuanto se lee disablement_code del dish
        "internet_check_interval_s": 60,  # cada cuanto se prueba la salida a internet
        "internet_check_hosts": ["1.1.1.1:443", "8.8.8.8:53"],  # alcanza con que responda uno
        "internet_check_timeout_s": 5,
        "telegram_queue_max": 50,         # avisos guardados mientras no hay internet
    },
    "history": {
        "retention_days": 90,
        "resolution_s": 60,  # cada cuanto se guarda un punto agregado en la DB
    },
    "weather": {
        "poll_interval_s": 900,  # 15 min - el pronostico de Open-Meteo no cambia mas seguido
    },
}

# Codigos WMO (weather_code de Open-Meteo) que consideramos "tormenta" para la
# alerta de clima: lluvia moderada/fuerte, chaparrones, tormenta electrica,
# tormenta con granizo. No incluye llovizna liviana (51-55) a proposito, para
# no generar falsos positivos con lluvia sin importancia.
STORM_WEATHER_CODES = {61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text())
            for k, v in user_cfg.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"Aviso: no se pudo leer config.json ({e}), uso los valores por defecto")
    return cfg


CONFIG = load_config()


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    """Devuelve True si Telegram acepto el mensaje (o si no hay Telegram
    configurado: no hay nada que reintentar), False si fallo el envio."""
    token = CONFIG.get("telegram_bot_token")
    chat_id = CONFIG.get("telegram_chat_id")
    if not token or not chat_id:
        return True
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Sin parse_mode: si el texto tiene caracteres especiales de Markdown,
        # la API de Telegram falla en silencio (devuelve ok:true pero no llega).
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print(f"No se pudo enviar alerta a Telegram: {e}")
        return False


class TelegramQueue:
    """Los avisos que no se pueden mandar (justo los importantes: sin
    internet no hay Telegram) quedan en cola y se reintentan en cada flush(),
    en orden, marcados con la hora en que se generaron. La cola tiene tope
    para no crecer sin limite en un corte largo: se descartan los mas viejos."""

    def __init__(self, max_len=50):
        self.max_len = max_len
        self.pending = []  # [(ts_generado, texto)]
        self.lock = threading.Lock()

    def notify(self, text, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if not self.pending and send_telegram(text):
                return
            self.pending.append((now, text))
            if len(self.pending) > self.max_len:
                self.pending = self.pending[-self.max_len:]

    def flush(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            while self.pending:
                ts, text = self.pending[0]
                if now - ts > 120:  # solo se marca si de verdad llega tarde
                    hora = time.strftime("%d/%m %H:%M", time.localtime(ts))
                    text = f"⏳ Aviso demorado (de las {hora}):\n{text}"
                if not send_telegram(text):
                    return
                self.pending.pop(0)


telegram_queue = TelegramQueue(max_len=CONFIG["alerts"].get("telegram_queue_max", 50))


# ── Clima (Open-Meteo, gratis, sin API key) ──────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherCache:
    """Consulta Open-Meteo para la ubicacion de config.json cada `interval`
    segundos (el pronostico no cambia mas seguido que eso). Guarda la
    nubosidad actual (para graficar junto a la obstruccion del dish) y los
    weather_code de las proximas horas (para que AlertManager pueda chequear
    si el pronostico confirma tormenta cuando la obstruccion empieza a subir).
    Sin latitud/longitud en config.json, simplemente no arranca."""

    def __init__(self, cfg, interval=900):
        self.lat = cfg.get("latitude")
        self.lon = cfg.get("longitude")
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = {"ok": False, "error": "todavia no se consulto"}

    def start(self):
        if self.lat is None or self.lon is None:
            print("WeatherCache: sin latitud/longitud en config.json, no se consulta el clima")
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                params = urllib.parse.urlencode({
                    "latitude": self.lat,
                    "longitude": self.lon,
                    "current": "cloud_cover,weather_code,precipitation",
                    "hourly": "weather_code,precipitation_probability",
                    "forecast_hours": 6,
                    "timezone": "auto",
                })
                req = urllib.request.Request(
                    f"{OPEN_METEO_URL}?{params}",
                    headers={"User-Agent": "starlink-panel/0.1 (+https://github.com/camammoli/starlink-panel)"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                with self.lock:
                    self.latest = {
                        "ok": True,
                        "ts": time.time(),
                        "cloud_cover": data.get("current", {}).get("cloud_cover"),
                        "precipitation": data.get("current", {}).get("precipitation"),
                        "current_weather_code": data.get("current", {}).get("weather_code"),
                        "hourly_weather_code": data.get("hourly", {}).get("weather_code", []),
                    }
            except Exception as e:
                with self.lock:
                    self.latest = {"ok": False, "error": str(e), "ts": time.time()}
            time.sleep(self.interval)

    def snapshot(self):
        with self.lock:
            return dict(self.latest)

    def storm_in_forecast(self, hours_ahead):
        """True si el codigo de clima actual o el de las proximas `hours_ahead`
        horas esta en STORM_WEATHER_CODES."""
        snap = self.snapshot()
        if not snap.get("ok"):
            return False
        codes = []
        if snap.get("current_weather_code") is not None:
            codes.append(snap["current_weather_code"])
        codes.extend(snap.get("hourly_weather_code", [])[:hours_ahead + 1])
        return any(c in STORM_WEATHER_CODES for c in codes)


# ── Alertas (estado + cooldown, para no repetir spam) ────────────────────────
HARDWARE_ALERT_LABELS = {
    "alert_thermal_throttle": "El dish se está recalentando (throttle térmico)",
    "alert_thermal_shutdown": "El dish se apagó por temperatura",
    "alert_mast_not_near_vertical": "El mástil no está vertical — revisar instalación",
    "alert_motors_stuck": "Motores del dish trabados",
    "alert_dish_water_detected": "Se detectó agua en el dish",
    "alert_router_water_detected": "Se detectó agua en el router",
}


class AlertManager:
    """Mira cada muestra nueva del dish y decide si corresponde mandar un
    aviso a Telegram. Todo con umbrales/tiempos sostenidos para no generar
    ruido (misma leccion aprendida con las alertas de Home Assistant)."""

    def __init__(self, cfg, weather_cache=None):
        self.cfg = cfg.get("alerts", {})
        self.weather_cache = weather_cache
        self.was_connected = None
        self.disconnected_since = None
        self.disconnect_alerted = False
        self.obstructed_since = None
        self.obstruction_alerted = False
        self.weather_alert_active = False
        self.hw_alert_state = {k: False for k in HARDWARE_ALERT_LABELS}
        self.service_problem_since = None
        self.service_problem_reason = None
        self.service_alerted = False

    def process_service(self, service, connected, now):
        """Servicio caido con el dish enganchado al satelite. Es lo que paso
        el 6/9/2026: cuenta suspendida por pago rechazado, el dish seguia
        "CONNECTED" y la alerta de desconexion no se entero. Dos senales:
        disablement_code del dish (directa) y una prueba de salida a internet
        (respaldo, por si el codigo no cambia). El aviso de inicio casi nunca
        sale en el momento (sin internet no hay Telegram): queda en la cola y
        llega junto con el de "restablecido", con las horas reales."""
        if not self.cfg.get("service_alert_enabled", True):
            return
        reason = service_problem(service, connected)
        if reason:
            if self.service_problem_since is None:
                self.service_problem_since = now
                self.service_problem_reason = reason
            elif not self.service_alerted and (now - self.service_problem_since) >= self.cfg.get("service_alert_after_s", 300):
                desde = time.strftime("%d/%m %H:%M", time.localtime(self.service_problem_since))
                telegram_queue.notify(f"🟠 Starlink sin servicio desde las {desde}: {self.service_problem_reason}.", now=now)
                self.service_alerted = True
        else:
            if self.service_alerted:
                desde = time.strftime("%d/%m %H:%M", time.localtime(self.service_problem_since))
                hasta = time.strftime("%d/%m %H:%M", time.localtime(now))
                telegram_queue.notify(
                    f"🟢 Servicio de Starlink restablecido. Estuvo sin servicio {fmt_duration(now - self.service_problem_since)} "
                    f"({desde} a {hasta}): {self.service_problem_reason}.", now=now)
            self.service_problem_since = None
            self.service_problem_reason = None
            self.service_alerted = False

    def process(self, sample):
        if not self.cfg.get("enabled", True):
            return
        if not sample.get("ok"):
            return
        status = sample["status"]
        now = sample["ts"]
        connected = status.get("state") == "CONNECTED"

        # Conexion caida / recuperada
        if connected:
            if self.disconnect_alerted and self.cfg.get("reconnect_notify", True):
                down_for = now - self.disconnected_since
                telegram_queue.notify(f"🟢 Starlink reconectado (estuvo caído {fmt_duration(down_for)}).")
            self.disconnected_since = None
            self.disconnect_alerted = False
        else:
            if self.disconnected_since is None:
                self.disconnected_since = now
            elif not self.disconnect_alerted and (now - self.disconnected_since) >= self.cfg.get("disconnect_after_s", 120):
                telegram_queue.notify(f"🔴 Starlink desconectado (estado: {status.get('state')}).")
                self.disconnect_alerted = True

        # Obstruccion sostenida
        frac = status.get("fraction_obstructed") or 0
        threshold = self.cfg.get("obstruction_threshold", 0.15)
        if frac >= threshold:
            if self.obstructed_since is None:
                self.obstructed_since = now
            elif not self.obstruction_alerted and (now - self.obstructed_since) >= self.cfg.get("obstruction_sustained_s", 300):
                telegram_queue.notify(f"🟡 Obstrucción alta y sostenida en el dish: {frac*100:.1f}%.")
                self.obstruction_alerted = True
        else:
            self.obstructed_since = None
            self.obstruction_alerted = False

        # Obstruccion en alza + pronostico confirma tormenta — chequeo aparte del
        # de "obstruccion sostenida" de arriba: este dispara mas rapido (no
        # espera un tiempo sostenido) porque el pronostico es la confirmacion,
        # no el tiempo. Edge-triggered: un chequeo por cada vez que cruza el
        # umbral, no uno por poll mientras se mantiene arriba.
        if self.cfg.get("weather_alert_enabled", True) and self.weather_cache is not None:
            w_threshold = self.cfg.get("weather_alert_obstruction_threshold", 0.10)
            if frac >= w_threshold:
                if not self.weather_alert_active:
                    hours = self.cfg.get("weather_alert_forecast_hours", 2)
                    if self.weather_cache.storm_in_forecast(hours):
                        telegram_queue.notify(
                            f"🌩️ Obstrucción del dish subiendo ({frac*100:.1f}%) y el pronóstico "
                            f"marca tormenta/lluvia ahora o en las próximas {hours}h — posible causa climática."
                        )
                    self.weather_alert_active = True
            else:
                self.weather_alert_active = False

        # Alertas de hardware — avisar en el flanco (False -> True), no en cada poll
        if self.cfg.get("hardware_alerts", True):
            active = sample.get("alerts", {})
            for key, label in HARDWARE_ALERT_LABELS.items():
                now_on = bool(active.get(key))
                if now_on and not self.hw_alert_state[key]:
                    telegram_queue.notify(f"⚠️ {label}.")
                self.hw_alert_state[key] = now_on


# disablement_code del dish (proto UserTerminalDisablementCode). OKAY = normal.
DISABLEMENT_LABELS = {
    "NO_ACTIVE_ACCOUNT": "cuenta sin servicio activo (¿suspensión por pago rechazado?)",
    "ACCOUNT_DISABLED": "cuenta deshabilitada",
    "DATA_OVERAGE_SANDBOX_POLICY": "se agotaron los datos del plan",
    "TOO_FAR_FROM_SERVICE_ADDRESS": "el dish está lejos de la dirección registrada",
    "OUTSIDE_HOME_REGION": "fuera de la región del plan",
    "ROAM_RESTRICTED": "roaming no permitido por el plan",
    "BLOCKED_COUNTRY": "país bloqueado",
    "BLOCKED_AREA": "zona bloqueada",
    "CELL_IS_DISABLED": "la celda de servicio está deshabilitada",
    "UNSUPPORTED_VERSION": "software del dish no soportado",
    "UNKNOWN_LOCATION": "ubicación desconocida",
}


def service_problem(service, connected):
    """Motivo (texto) si el servicio esta caido; None si esta bien o no se sabe."""
    if not service:
        return None
    code = service.get("disablement_code")
    if code and code not in ("OKAY", "UNKNOWN_STATE"):
        return f"{DISABLEMENT_LABELS.get(code, 'servicio deshabilitado por Starlink')} [{code}]"
    if connected and service.get("internet_ok") is False:
        return "el dish está conectado al satélite pero no hay salida a internet"
    return None


def internet_reachable(hosts, timeout):
    """True si alguno de los host:puerto acepta una conexion TCP."""
    for hp in hosts:
        host, _, port = hp.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class ServiceMonitor:
    """Lee disablement_code del dish (no viene en status_data(), hay que pedir
    el status crudo) y prueba la salida a internet, cada uno con su propio
    intervalo, en un hilo aparte para no demorar el poll de 2.5s."""

    def __init__(self, target, cfg):
        self.target = target
        self.cfg = cfg.get("alerts", {})
        self.lock = threading.Lock()
        self.latest = {"disablement_code": None, "internet_ok": None, "ts": None, "internet_ts": None}

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        ctx = starlink_grpc.ChannelContext(target=self.target)
        code_every = self.cfg.get("service_check_interval_s", 30)
        net_every = self.cfg.get("internet_check_interval_s", 60)
        last_code = last_net = 0.0
        while True:
            now = time.time()
            if now - last_code >= code_every:
                last_code = now
                try:
                    st = starlink_grpc.get_status(ctx)
                    enum = st.DESCRIPTOR.fields_by_name["disablement_code"].enum_type
                    code = enum.values_by_number[st.disablement_code].name
                except Exception:
                    code = None  # dish inalcanzable: eso lo cubre la alerta de desconexion
                with self.lock:
                    self.latest["disablement_code"] = code
                    self.latest["ts"] = now
            if now - last_net >= net_every:
                last_net = now
                ok = internet_reachable(self.cfg.get("internet_check_hosts", ["1.1.1.1:443", "8.8.8.8:53"]),
                                        self.cfg.get("internet_check_timeout_s", 5))
                with self.lock:
                    self.latest["internet_ok"] = ok
                    self.latest["internet_ts"] = now
            time.sleep(5)

    def snapshot(self):
        with self.lock:
            return dict(self.latest)


def fmt_duration(seconds):
    m = int(seconds // 60)
    if m < 1:
        return f"{int(seconds)}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ── Historial persistente (SQLite) ───────────────────────────────────────────
class HistoryStore:
    """Guarda un punto agregado cada `resolution_s` segundos con el promedio
    de las muestras de esa ventana — no cada muestra individual, para que la
    base no crezca sin limite. Aplica retencion (borra lo mas viejo que
    `retention_days`) en cada insercion."""

    def __init__(self, resolution_s, retention_days):
        self.resolution_s = resolution_s
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self.bucket = []
        self.bucket_start = None
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS samples (
                    ts INTEGER PRIMARY KEY,
                    latency_ms REAL,
                    down_bps REAL,
                    up_bps REAL,
                    drop_rate REAL,
                    obstructed_fraction REAL,
                    state TEXT
                )
            """)
            # Migracion: agregar columnas nuevas si la tabla ya existia de antes
            # (CREATE TABLE IF NOT EXISTS no toca una tabla ya creada) — asi no
            # se pierde el historial ya acumulado al agregar columnas.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()]
            if "cloud_cover" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN cloud_cover REAL")
            if "precipitation" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN precipitation REAL")

    def _conn(self):
        return sqlite3.connect(DB_FILE)

    def feed(self, sample, cloud_cover=None, precipitation=None):
        if not sample.get("ok"):
            return
        status = sample["status"]
        now = sample["ts"]
        with self.lock:
            # Si esta muestra ya cae fuera de la ventana actual, cerrar esa
            # ventana con lo que tenia ANTES de sumar la muestra nueva — si no,
            # la muestra que dispara el corte terminaba promediada con la
            # ventana vieja en vez de arrancar la siguiente.
            if self.bucket_start is not None and (now - self.bucket_start) >= self.resolution_s:
                self._flush(self.bucket_start)
                self.bucket = []
                self.bucket_start = None

            if self.bucket_start is None:
                self.bucket_start = now
            self.bucket.append({
                "latency_ms": status.get("pop_ping_latency_ms"),
                "down_bps": status.get("downlink_throughput_bps"),
                "up_bps": status.get("uplink_throughput_bps"),
                "drop_rate": status.get("pop_ping_drop_rate"),
                "obstructed_fraction": status.get("fraction_obstructed"),
                "state": status.get("state"),
                "cloud_cover": cloud_cover,
                "precipitation": precipitation,
            })

    def _flush(self, ts):
        if not self.bucket:
            return

        def avg(key):
            vals = [b[key] for b in self.bucket if b[key] is not None]
            return sum(vals) / len(vals) if vals else None

        states = [b["state"] for b in self.bucket if b["state"]]
        state = max(set(states), key=states.count) if states else None

        row = (int(ts), avg("latency_ms"), avg("down_bps"), avg("up_bps"), avg("drop_rate"), avg("obstructed_fraction"), state, avg("cloud_cover"), avg("precipitation"))
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO samples (ts, latency_ms, down_bps, up_bps, drop_rate, obstructed_fraction, state, cloud_cover, precipitation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                cutoff = int(ts - self.retention_days * 86400)
                conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        except Exception as e:
            print(f"No se pudo escribir historial: {e}")

    def query(self, ts_from, ts_to, limit=5000):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT ?",
                (ts_from, ts_to, limit),
            ).fetchall()
            return [dict(r) for r in rows]


# ── TLE de satelites (cacheado server-side, ver comentario abajo) ───────────
class TLECache:
    """Descarga los TLE de satelites Starlink desde Celestrak UNA VEZ por
    ventana de 2h, sin importar cuantos navegadores esten mirando el panel.

    Celestrak aplica "una descarga por actualizacion" y esta politica parece
    ser global por dataset (no por IP de cliente) - confirmado viendo el mismo
    timestamp de "ultima descarga" desde dos redes distintas. Si cada
    navegador de cada visitante pidiera el TLE por su cuenta, varios usuarios
    del mismo panel se bloquearian entre si. Cachear aca, server-side, respeta
    la politica de verdad y sirve a todos los que abran el panel desde el
    mismo dato ya bajado.

    Se persiste en disco (tle_cache.json) para no perder el cache — y con eso
    la ventana de 2h — si el servicio se reinicia.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.text = None
        self.fetched_at = 0.0
        self._load_from_disk()

    def _load_from_disk(self):
        try:
            data = json.loads(TLE_CACHE_FILE.read_text())
            self.text = data.get("text")
            self.fetched_at = data.get("fetched_at", 0.0)
        except Exception:
            pass

    def _save_to_disk(self):
        try:
            TLE_CACHE_FILE.write_text(json.dumps({"text": self.text, "fetched_at": self.fetched_at}))
        except Exception:
            pass

    def get(self):
        with self.lock:
            age = time.time() - self.fetched_at
            if self.text is not None and age < TLE_MAX_AGE_S:
                return {"ok": True, "text": self.text, "age_s": age, "stale": False}
            try:
                req = urllib.request.Request(
                    CELESTRAK_URL,
                    headers={"User-Agent": "starlink-panel/0.1 (+https://github.com/camammoli/starlink-panel)"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    text = resp.read().decode("utf-8")
                self.text = text
                self.fetched_at = time.time()
                self._save_to_disk()
                return {"ok": True, "text": self.text, "age_s": 0, "stale": False}
            except Exception as e:
                if self.text is not None:
                    return {"ok": True, "text": self.text, "age_s": age, "stale": True, "warning": str(e)}
                return {"ok": False, "error": str(e)}


tle_cache = TLECache()
weather_cache = WeatherCache(CONFIG, interval=CONFIG["weather"]["poll_interval_s"])
alert_manager = AlertManager(CONFIG, weather_cache=weather_cache)
history_store = HistoryStore(
    resolution_s=CONFIG["history"]["resolution_s"],
    retention_days=CONFIG["history"]["retention_days"],
)


class DishPoller:
    """Consulta el dish en un hilo de fondo y guarda el ultimo estado +
    un historial corto en memoria, protegido por un lock simple. Cada muestra
    tambien se pasa al AlertManager y al HistoryStore."""

    def __init__(self, target, interval=2.5):
        self.target = target
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = {"ok": False, "error": "todavia no se consulto", "ts": time.time()}
        self.history = []
        self.last_flush = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        ctx = starlink_grpc.ChannelContext(target=self.target)
        while True:
            try:
                status, obstruction, alerts = starlink_grpc.status_data(ctx)
                sample = {
                    "ok": True,
                    "ts": time.time(),
                    "status": status,
                    "obstruction": obstruction,
                    "alerts": {k: v for k, v in alerts.items() if v},
                }
                with self.lock:
                    self.latest = sample
                    self.history.append({
                        "ts": sample["ts"],
                        "latency_ms": status.get("pop_ping_latency_ms"),
                        "down_bps": status.get("downlink_throughput_bps"),
                        "up_bps": status.get("uplink_throughput_bps"),
                        "drop_rate": status.get("pop_ping_drop_rate"),
                    })
                    if len(self.history) > HISTORY_MAXLEN:
                        self.history = self.history[-HISTORY_MAXLEN:]
            except Exception as e:  # dish offline, red caida, etc. - no morir
                sample = {"ok": False, "error": str(e), "ts": time.time()}
                with self.lock:
                    self.latest = sample

            try:
                alert_manager.process(sample)
                if service_monitor is not None:
                    connected = bool(sample.get("ok")) and sample["status"].get("state") == "CONNECTED"
                    alert_manager.process_service(service_monitor.snapshot(), connected, sample["ts"])
                if telegram_queue.pending and sample["ts"] - self.last_flush >= 30:
                    self.last_flush = sample["ts"]
                    telegram_queue.flush(sample["ts"])
            except Exception as e:
                print(f"AlertManager error: {e}")
            try:
                weather_snap = weather_cache.snapshot()
                history_store.feed(
                    sample,
                    cloud_cover=weather_snap.get("cloud_cover"),
                    precipitation=weather_snap.get("precipitation"),
                )
            except Exception as e:
                print(f"HistoryStore error: {e}")

            time.sleep(self.interval)

    def snapshot(self):
        with self.lock:
            return {"latest": self.latest, "history": list(self.history)}


class ObstructionMapCache:
    """Consulta el mapa de obstrucciones (grilla de SNR por dirección, la
    misma data que arma el mapa visual de la app oficial) cada `interval`
    segundos en un hilo aparte. No hace falta consultarlo tan seguido como el
    status (cambia lento — el dish tarda en acumular datos por dirección),
    así que va separado del DishPoller para no cargar cada poll de 2.5s con
    una grilla de ~15000 celdas."""

    def __init__(self, target, interval=30):
        self.target = target
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = {"ok": False, "error": "todavia no se consulto", "ts": time.time()}

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        ctx = starlink_grpc.ChannelContext(target=self.target)
        while True:
            try:
                grid = starlink_grpc.obstruction_map(ctx)
                rows = len(grid)
                cols = len(grid[0]) if rows else 0
                with self.lock:
                    self.latest = {
                        "ok": True,
                        "rows": rows,
                        "cols": cols,
                        "grid": [list(r) for r in grid],
                        "ts": time.time(),
                    }
            except Exception as e:
                with self.lock:
                    self.latest = {"ok": False, "error": str(e), "ts": time.time()}
            time.sleep(self.interval)

    def snapshot(self):
        with self.lock:
            return dict(self.latest)


poller: DishPoller = None  # type: ignore
obstruction_cache: ObstructionMapCache = None  # type: ignore
service_monitor: ServiceMonitor = None  # type: ignore


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencioso

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_params(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))

    def do_GET(self):
        route = self.path.split("?")[0]

        if route == "/api/status":
            data = poller.snapshot()
            svc = service_monitor.snapshot()
            latest = data["latest"]
            connected = bool(latest.get("ok")) and latest["status"].get("state") == "CONNECTED"
            svc["problem"] = service_problem(svc, connected)
            data["service"] = svc
            self._send_json(data)
            return

        if route == "/api/obstruction-map":
            self._send_json(obstruction_cache.snapshot())
            return

        if route == "/api/tle":
            self._send_json(tle_cache.get())
            return

        if route == "/api/config":
            # Solo lo que el frontend necesita — nunca el token de Telegram.
            self._send_json({
                "latitude": CONFIG.get("latitude"),
                "longitude": CONFIG.get("longitude"),
            })
            return

        if route == "/api/history":
            params = self._query_params()
            now = time.time()
            ts_from = float(params.get("from", now - 86400))
            ts_to = float(params.get("to", now))
            rows = history_store.query(ts_from, ts_to)
            self._send_json({"rows": rows})
            return

        if route == "/api/history/export":
            params = self._query_params()
            now = time.time()
            ts_from = float(params.get("from", now - 86400))
            ts_to = float(params.get("to", now))
            rows = history_store.query(ts_from, ts_to, limit=1000000)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["ts", "fecha", "latency_ms", "down_bps", "up_bps", "drop_rate", "obstructed_fraction", "state", "cloud_cover", "precipitation"])
            for r in rows:
                fecha = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
                writer.writerow([r["ts"], fecha, r["latency_ms"], r["down_bps"], r["up_bps"], r["drop_rate"], r["obstructed_fraction"], r["state"], r.get("cloud_cover"), r.get("precipitation")])
            body = buf.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=starlink-historial.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        path = route
        if path == "/":
            path = "/index.html"
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()

        # No servir nada fuera de static/ (path traversal)
        if not (file_path == STATIC_DIR or file_path.is_relative_to(STATIC_DIR)):
            self.send_response(403)
            self.end_headers()
            return
        if not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global poller, obstruction_cache, service_monitor
    parser = argparse.ArgumentParser(description="Panel de estado de Starlink")
    parser.add_argument("--port", type=int, default=8850)
    parser.add_argument("--dish", default="192.168.100.1:9200", help="IP:puerto del dish")
    parser.add_argument("--interval", type=float, default=2.5, help="Segundos entre consultas al dish")
    parser.add_argument("--obstruction-interval", type=float, default=30,
                         help="Segundos entre consultas al mapa de obstrucciones")
    args = parser.parse_args()

    service_monitor = ServiceMonitor(args.dish, CONFIG)
    service_monitor.start()

    poller = DishPoller(args.dish, interval=args.interval)
    poller.start()

    obstruction_cache = ObstructionMapCache(args.dish, interval=args.obstruction_interval)
    obstruction_cache.start()

    weather_cache.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Starlink Panel: http://0.0.0.0:{args.port}  (dish: {args.dish})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
