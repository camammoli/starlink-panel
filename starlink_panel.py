#!/usr/bin/env python3
"""Starlink Panel — dashboard independiente de estado de tu Starlink.

Corre en cualquier dispositivo conectado a la MISMA RED que tu Starlink: el
dish siempre usa la IP fija 192.168.100.1:9200 para su API local de
diagnostico, asi que no hace falta configurar nada especifico de tu red.

Instalacion:
    pip install grpcio starlink-grpc-core

Uso:
    python3 starlink_panel.py [--port 8850] [--dish 192.168.100.1:9200]

Despues abri http://<esta-maquina>:<puerto> desde cualquier navegador de tu
red local.
"""
import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import starlink_grpc

STATIC_DIR = (Path(__file__).parent / "static").resolve()
HISTORY_MAXLEN = 240  # a intervalo default de 2.5s, ~10 minutos de historial

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle"
TLE_CACHE_FILE = Path(__file__).parent / "tle_cache.json"
TLE_MAX_AGE_S = 2 * 60 * 60  # Celestrak actualiza cada 2h y aplica 1 descarga por ventana


class TLECache:
    """Descarga los TLE de satelites Starlink desde Celestrak UNA VEZ por
    ventana de 2h, sin importar cuantos navegadores esten mirando el panel.

    Celestrak aplica "una descarga por actualizacion" y esta politica parece
    ser global por dataset (no por IP de cliente) - confirmado viendo el mismo
    timestamp de "ultima descarga" desde dos redes distintas. Si cada
    navegador de cada visitante pidiera el TLE por su cuenta (como en la v0.1
    original), varios usuarios del mismo panel se bloquearian entre si. Cachear
    aca, server-side, respeta la politica de verdad y sirve a todos los que
    abran el panel desde el mismo dato ya bajado.

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
                    # Celestrak no respondio (o nos bloqueo) - servimos el
                    # cache viejo en vez de dejar al usuario sin nada.
                    return {"ok": True, "text": self.text, "age_s": age, "stale": True, "warning": str(e)}
                return {"ok": False, "error": str(e)}


tle_cache = TLECache()


class DishPoller:
    """Consulta el dish en un hilo de fondo y guarda el ultimo estado +
    un historial corto en memoria, protegido por un lock simple."""

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
                with self.lock:
                    self.latest = {"ok": False, "error": str(e), "ts": time.time()}
            time.sleep(self.interval)

    def snapshot(self):
        with self.lock:
            return {"latest": self.latest, "history": list(self.history)}


poller: DishPoller = None  # type: ignore


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

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            self._send_json(poller.snapshot())
            return

        if self.path.split("?")[0] == "/api/tle":
            self._send_json(tle_cache.get())
            return

        path = self.path.split("?")[0]
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
    global poller
    parser = argparse.ArgumentParser(description="Panel de estado de Starlink")
    parser.add_argument("--port", type=int, default=8850)
    parser.add_argument("--dish", default="192.168.100.1:9200", help="IP:puerto del dish")
    parser.add_argument("--interval", type=float, default=2.5, help="Segundos entre consultas al dish")
    args = parser.parse_args()

    poller = DishPoller(args.dish, interval=args.interval)
    poller.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Starlink Panel: http://0.0.0.0:{args.port}  (dish: {args.dish})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
