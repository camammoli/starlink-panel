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
    },
    "history": {
        "retention_days": 90,
        "resolution_s": 60,  # cada cuanto se guarda un punto agregado en la DB
    },
}


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
    token = CONFIG.get("telegram_bot_token")
    chat_id = CONFIG.get("telegram_chat_id")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Sin parse_mode: si el texto tiene caracteres especiales de Markdown,
        # la API de Telegram falla en silencio (devuelve ok:true pero no llega).
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"No se pudo enviar alerta a Telegram: {e}")


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

    def __init__(self, cfg):
        self.cfg = cfg.get("alerts", {})
        self.was_connected = None
        self.disconnected_since = None
        self.disconnect_alerted = False
        self.obstructed_since = None
        self.obstruction_alerted = False
        self.hw_alert_state = {k: False for k in HARDWARE_ALERT_LABELS}

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
                send_telegram(f"🟢 Starlink reconectado (estuvo caído {fmt_duration(down_for)}).")
            self.disconnected_since = None
            self.disconnect_alerted = False
        else:
            if self.disconnected_since is None:
                self.disconnected_since = now
            elif not self.disconnect_alerted and (now - self.disconnected_since) >= self.cfg.get("disconnect_after_s", 120):
                send_telegram(f"🔴 Starlink desconectado (estado: {status.get('state')}).")
                self.disconnect_alerted = True

        # Obstruccion sostenida
        frac = status.get("fraction_obstructed") or 0
        threshold = self.cfg.get("obstruction_threshold", 0.15)
        if frac >= threshold:
            if self.obstructed_since is None:
                self.obstructed_since = now
            elif not self.obstruction_alerted and (now - self.obstructed_since) >= self.cfg.get("obstruction_sustained_s", 300):
                send_telegram(f"🟡 Obstrucción alta y sostenida en el dish: {frac*100:.1f}%.")
                self.obstruction_alerted = True
        else:
            self.obstructed_since = None
            self.obstruction_alerted = False

        # Alertas de hardware — avisar en el flanco (False -> True), no en cada poll
        if self.cfg.get("hardware_alerts", True):
            active = sample.get("alerts", {})
            for key, label in HARDWARE_ALERT_LABELS.items():
                now_on = bool(active.get(key))
                if now_on and not self.hw_alert_state[key]:
                    send_telegram(f"⚠️ {label}.")
                self.hw_alert_state[key] = now_on


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

    def _conn(self):
        return sqlite3.connect(DB_FILE)

    def feed(self, sample):
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
            })

    def _flush(self, ts):
        if not self.bucket:
            return

        def avg(key):
            vals = [b[key] for b in self.bucket if b[key] is not None]
            return sum(vals) / len(vals) if vals else None

        states = [b["state"] for b in self.bucket if b["state"]]
        state = max(set(states), key=states.count) if states else None

        row = (int(ts), avg("latency_ms"), avg("down_bps"), avg("up_bps"), avg("drop_rate"), avg("obstructed_fraction"), state)
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO samples (ts, latency_ms, down_bps, up_bps, drop_rate, obstructed_fraction, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
alert_manager = AlertManager(CONFIG)
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
            except Exception as e:
                print(f"AlertManager error: {e}")
            try:
                history_store.feed(sample)
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
            self._send_json(poller.snapshot())
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
            writer.writerow(["ts", "fecha", "latency_ms", "down_bps", "up_bps", "drop_rate", "obstructed_fraction", "state"])
            for r in rows:
                fecha = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
                writer.writerow([r["ts"], fecha, r["latency_ms"], r["down_bps"], r["up_bps"], r["drop_rate"], r["obstructed_fraction"], r["state"]])
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
    global poller, obstruction_cache
    parser = argparse.ArgumentParser(description="Panel de estado de Starlink")
    parser.add_argument("--port", type=int, default=8850)
    parser.add_argument("--dish", default="192.168.100.1:9200", help="IP:puerto del dish")
    parser.add_argument("--interval", type=float, default=2.5, help="Segundos entre consultas al dish")
    parser.add_argument("--obstruction-interval", type=float, default=30,
                         help="Segundos entre consultas al mapa de obstrucciones")
    args = parser.parse_args()

    poller = DishPoller(args.dish, interval=args.interval)
    poller.start()

    obstruction_cache = ObstructionMapCache(args.dish, interval=args.obstruction_interval)
    obstruction_cache.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Starlink Panel: http://0.0.0.0:{args.port}  (dish: {args.dish})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
