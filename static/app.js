// ── Utilidades de formato ────────────────────────────────────────────────────
function fmtMbps(bps) {
  if (bps == null) return '—';
  return `${(bps / 1e6).toLocaleString('es-AR', { maximumFractionDigits: 2 })} Mbps`;
}
function fmtMs(ms) {
  if (ms == null) return '—';
  return `${ms.toLocaleString('es-AR', { maximumFractionDigits: 1 })} ms`;
}
function fmtPct(frac) {
  if (frac == null) return '—';
  return `${(frac * 100).toLocaleString('es-AR', { maximumFractionDigits: 2 })}%`;
}
function fmtUptime(s) {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function norm360(deg) { return ((deg % 360) + 360) % 360; }

// ── Estado de conexión + tarjetas ────────────────────────────────────────────
const STATE_LABELS = {
  CONNECTED: ['ok', 'Conectado'],
  SEARCHING: ['warn', 'Buscando satélite'],
  BOOTING: ['warn', 'Iniciando'],
  STOWED: ['warn', 'Guardado (stow)'],
  THERMAL_SHUTDOWN: ['err', 'Apagado por calor'],
  NO_SATS: ['err', 'Sin satélites'],
  OBSTRUCTED: ['err', 'Obstruido'],
  NO_DOWNLINK: ['err', 'Sin bajada'],
  NO_PINGS: ['err', 'Sin respuesta'],
};

// Alertas que el dish reporta como parte de su operación normal — no son
// problemas, así que se muestran aparte (tono neutro) en vez de mezclarse
// con las que sí requieren atención.
const INFO_ALERTS = {
  alert_install_pending: 'Hay una actualización de software descargada, lista para instalarse en el próximo reinicio (corta internet un momento).',
  alert_is_heating: 'El dish está calentándose (normal en frío, por ejemplo para derretir nieve/escarcha).',
  alert_is_power_save_idle: 'El dish entró en modo de ahorro de energía por inactividad — normal.',
  alert_roaming: 'El dish está siendo usado fuera de su dirección de servicio registrada.',
  alert_dbf_telem_stale: 'Telemetría interna del dish desactualizada — informativo, no requiere acción.',
};

function updateCards(latest) {
  const badge = document.getElementById('conn-badge');
  const text = document.getElementById('conn-text');

  if (!latest.ok) {
    badge.className = 'badge err';
    text.textContent = 'sin conexión con el dish';
    document.getElementById('device-info').textContent = latest.error || '';
    return;
  }

  const s = latest.status;
  const [cls, label] = STATE_LABELS[s.state] || ['warn', s.state];
  badge.className = `badge ${cls}`;
  text.textContent = label;

  document.getElementById('device-info').textContent =
    `${s.hardware_version || ''} · sw ${s.software_version || ''} · id ${s.id || ''}`;

  document.getElementById('v-latency').textContent = fmtMs(s.pop_ping_latency_ms);
  document.getElementById('v-down').textContent = fmtMbps(s.downlink_throughput_bps);
  document.getElementById('v-up').textContent = fmtMbps(s.uplink_throughput_bps);
  document.getElementById('v-drop').textContent = fmtPct(s.pop_ping_drop_rate);
  document.getElementById('v-obstr').innerHTML =
    fmtPct(s.fraction_obstructed) + (s.currently_obstructed ? ' <small>(ahora)</small>' : '');
  document.getElementById('v-uptime').textContent = fmtUptime(s.uptime);
  document.getElementById('v-gps').innerHTML =
    (s.gps_ready ? `${s.gps_sats} sats` : 'no listo') + (s.gps_enabled ? '' : ' <small>(deshabilitado)</small>');
  document.getElementById('v-snr').textContent = s.is_snr_above_noise_floor == null
    ? '—' : (s.is_snr_above_noise_floor ? 'OK' : 'bajo');
  document.getElementById('v-obstr-detail').innerHTML = (s.obstruction_duration == null)
    ? '<small>sin datos</small>'
    : `${s.obstruction_duration.toFixed(1)}s <small>cada ~${Math.round(s.obstruction_interval / 60)}min</small>`;

  const alertsBox = document.getElementById('alerts-box');
  const activeAlerts = Object.keys(latest.alerts || {});
  const realAlerts = activeAlerts.filter(a => !INFO_ALERTS[a]);
  const infoAlerts = activeAlerts.filter(a => INFO_ALERTS[a]);

  let html = '';
  if (!realAlerts.length) {
    html += '<div class="empty">Sin alertas.</div>';
  } else {
    html += realAlerts
      .map(a => `<div class="alert-item">${a.replace('alert_', '').replaceAll('_', ' ')}</div>`)
      .join('');
  }
  if (infoAlerts.length) {
    html += infoAlerts
      .map(a => `<div class="alert-item alert-info">ℹ️ ${INFO_ALERTS[a]}</div>`)
      .join('');
  }
  alertsBox.innerHTML = html;

  window._dishDirection = { az: s.direction_azimuth, el: s.direction_elevation };
}

// ── Gráfico de latencia / velocidad ──────────────────────────────────────────
function drawChart(history) {
  const canvas = document.getElementById('chart');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const pts = history.filter(h => h.latency_ms != null);
  if (pts.length < 2) {
    ctx.fillStyle = '#9aa3af';
    ctx.font = '12px system-ui';
    ctx.fillText('Esperando datos…', 10, H / 2);
    return;
  }

  function series(key, color) {
    const vals = pts.map(p => p[key]).filter(v => v != null);
    if (!vals.length) return;
    const min = Math.min(...vals), max = Math.max(...vals) || 1;
    const range = (max - min) || 1;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = (i / (pts.length - 1)) * (W - 10) + 5;
      const norm = p[key] == null ? 0 : (p[key] - min) / range;
      const y = H - 10 - norm * (H - 20);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  series('latency_ms', '#4a9eff');
  series('down_bps', '#4ac97a');

  ctx.font = '11px system-ui';
  ctx.fillStyle = '#4a9eff';
  ctx.fillText('latencia', 8, 14);
  ctx.fillStyle = '#4ac97a';
  ctx.fillText('bajada', 70, 14);
}

// ── Sky plot: dirección real del dish + satélites Starlink visibles ─────────
// Mapa de obstrucciones: grilla de SNR (0=obstruido, 1=libre, -1=sin datos)
// tal como la arma el dish, en la MISMA proyección circular (centro=cenit,
// borde del circulo=horizonte, norte arriba) que ya usa este mismo canvas
// para el apuntado del dish y los satelites — se puede dibujar directo sin
// convertir a azimut/elevacion, fila/columna ya son x/y de esa proyección.
function drawObstructionMap(ctx, cx, cy, R, map) {
  if (!map || !map.ok || !map.grid || !map.grid.length) return;
  const rows = map.rows, cols = map.cols;
  const cellW = (2 * R) / cols, cellH = (2 * R) / rows;
  for (let i = 0; i < rows; i++) {
    const py = (i / (rows - 1)) * 2 - 1;
    const row = map.grid[i];
    for (let j = 0; j < cols; j++) {
      const v = row[j];
      if (v < 0) continue; // sin datos en esa dirección todavía
      const px = (j / (cols - 1)) * 2 - 1;
      if (px * px + py * py > 1) continue; // fuera del círculo válido
      const x = cx + px * R, y = cy + py * R;
      // v=1 (libre) -> verde, v=0 (obstruido) -> rojo
      const r = Math.round(224 * (1 - v) + 74 * v);
      const g = Math.round(90 * (1 - v) + 201 * v);
      const b = Math.round(74 * (1 - v) + 122 * v);
      ctx.fillStyle = `rgba(${r},${g},${b},0.55)`;
      ctx.fillRect(x - cellW / 2, y - cellH / 2, cellW + 0.5, cellH + 0.5);
    }
  }
}

function drawSky(dishDir, satellites, obstructionMap) {
  const canvas = document.getElementById('sky');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 16;
  ctx.clearRect(0, 0, W, H);

  drawObstructionMap(ctx, cx, cy, R, obstructionMap);

  // Anillos de elevación (0/30/60/90) y marcas cardinales — trazo mas claro
  // que antes para que se sigan viendo por encima del mapa de obstrucciones
  ctx.strokeStyle = 'rgba(255,255,255,0.25)';
  ctx.fillStyle = '#9aa3af';
  ctx.font = '10px system-ui';
  [0, 30, 60].forEach(el => {
    const r = R * (1 - el / 90);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
  });
  ['N', 'E', 'S', 'O'].forEach((label, i) => {
    const ang = (i * 90 - 90) * Math.PI / 180;
    ctx.fillText(label, cx + Math.cos(ang) * (R + 8) - 3, cy + Math.sin(ang) * (R + 8) + 3);
  });

  function toXY(azDeg, elDeg) {
    const az = norm360(azDeg);
    const r = R * (1 - Math.max(0, elDeg) / 90);
    const ang = (az - 90) * Math.PI / 180; // 0°=N arriba, horario
    return [cx + Math.cos(ang) * r, cy + Math.sin(ang) * r];
  }

  // Satélites visibles (fondo)
  (satellites || []).forEach(sat => {
    if (sat.el <= 0) return;
    const [x, y] = toXY(sat.az, sat.el);
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#e0a934';
    ctx.fill();
  });

  // Dirección real del dish (encima, más grande)
  if (dishDir && dishDir.az != null && dishDir.el != null) {
    const [x, y] = toXY(dishDir.az, dishDir.el);
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fillStyle = '#4a9eff';
    ctx.fill();
    ctx.strokeStyle = '#0f1115';
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

// ── Polling del backend ──────────────────────────────────────────────────────
async function poll() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    updateCards(data.latest);
    drawChart(data.history);
    drawSky(window._dishDirection, window._visibleSats, window._obstructionMap);
  } catch (e) {
    // backend caido (raro, corre local) - reintenta en el proximo tick
  }
}
poll();
setInterval(poll, 2500);

// El mapa de obstrucciones cambia lento del lado del dish (el backend lo
// actualiza cada 30s) — pedirlo con el mismo intervalo alcanza, no hace
// falta bajar ~15000 celdas en cada poll de 2.5s.
async function pollObstructionMap() {
  try {
    const res = await fetch('/api/obstruction-map');
    window._obstructionMap = await res.json();
  } catch (e) { /* se reintenta en el proximo tick */ }
}
pollObstructionMap();
setInterval(pollObstructionMap, 30000);

// ── Satélites Starlink visibles (TLE vía nuestro propio backend) ────────────
// El backend (/api/tle) es el que le pega a Celestrak, UNA vez cada 2h,
// cacheado en el servidor — así todos los navegadores que abran este panel
// comparten la misma descarga en vez de competir por la cuota de Celestrak
// entre sí (ver comentario en starlink_panel.py). Acá solo consumimos.
async function loadTLEs() {
  const res = await fetch('/api/tle');
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || 'error desconocido');
  if (data.stale) {
    console.warn('TLE de Celestrak desactualizado, usando el último cache disponible:', data.warning);
  }
  return data.text;
}

function parseTLEs(text) {
  const lines = text.split('\n').map(l => l.trimEnd()).filter(Boolean);
  const sats = [];
  for (let i = 0; i + 2 < lines.length; i += 3) {
    sats.push({ name: lines[i].trim(), l1: lines[i + 1], l2: lines[i + 2] });
  }
  return sats;
}

let satrecs = [];
let observerGd = null;

function computeVisible() {
  if (!observerGd || !satrecs.length || typeof satellite === 'undefined') return;
  const now = new Date();
  const gmst = satellite.gstime(now);
  const visible = [];
  for (const s of satrecs) {
    try {
      const pv = satellite.propagate(s.rec, now);
      if (!pv || !pv.position) continue;
      const ecf = satellite.eciToEcf(pv.position, gmst);
      const look = satellite.ecfToLookAngles(observerGd, ecf);
      const elDeg = look.elevation * 180 / Math.PI;
      if (elDeg > 0) {
        visible.push({ az: look.azimuth * 180 / Math.PI, el: elDeg, name: s.name });
      }
    } catch (e) { /* TLE puntual invalido, ignorar ese satelite */ }
  }
  window._visibleSats = visible;
  document.getElementById('sky-hint').textContent =
    `${visible.length} satélites Starlink sobre tu horizonte ahora mismo.`;
}

let skyInterval = null;

async function startSkyTracking(lat, lon) {
  const hint = document.getElementById('sky-hint');
  observerGd = {
    longitude: lon * Math.PI / 180,
    latitude: lat * Math.PI / 180,
    height: 0.3, // km sobre el elipsoide, aproximado
  };
  hint.textContent = 'Descargando posiciones de satélites Starlink (Celestrak)…';
  try {
    const text = await loadTLEs();
    const parsed = parseTLEs(text);
    satrecs = parsed.map(s => ({ name: s.name, rec: satellite.twoline2satrec(s.l1, s.l2) }));
    computeVisible();
    if (skyInterval) clearInterval(skyInterval);
    skyInterval = setInterval(computeVisible, 5000);
  } catch (e) {
    hint.textContent = 'No se pudo descargar la lista de satélites (Celestrak) — reintentá más tarde.';
  }
}

async function enableSky() {
  const hint = document.getElementById('sky-hint');

  // La Geolocation API requiere "contexto seguro" (HTTPS o localhost) — en
  // HTTP normal (típico de un panel casero en la red local, ej. http://loli.lan)
  // los navegadores la bloquean SIN mostrar ningún diálogo de permiso, lo que
  // confunde porque parece "denegado" sin haberlo pedido nunca. En ese caso
  // vamos directo al formulario manual en vez de intentar algo que va a fallar
  // silenciosamente.
  if (!window.isSecureContext) {
    document.getElementById('manual-geo').style.display = 'block';
    hint.textContent = 'Ingresá tu ubicación manualmente (ver el cuadro de abajo).';
    return;
  }

  if (!navigator.geolocation) {
    hint.textContent = 'Este navegador no soporta geolocalización.';
    document.getElementById('manual-geo').style.display = 'block';
    return;
  }
  hint.textContent = 'Obteniendo tu ubicación…';
  navigator.geolocation.getCurrentPosition(
    (pos) => startSkyTracking(pos.coords.latitude, pos.coords.longitude),
    () => {
      hint.textContent = 'No se pudo obtener tu ubicación (permiso denegado).';
      document.getElementById('manual-geo').style.display = 'block';
    }
  );
}

document.getElementById('btn-geo').addEventListener('click', enableSky);

document.getElementById('btn-geo-manual').addEventListener('click', () => {
  const hint = document.getElementById('sky-hint');
  const lat = parseFloat(document.getElementById('geo-lat').value);
  const lon = parseFloat(document.getElementById('geo-lon').value);
  if (isNaN(lat) || isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    hint.textContent = 'Coordenadas inválidas — latitud entre -90 y 90, longitud entre -180 y 180.';
    return;
  }
  startSkyTracking(lat, lon);
});

// Si config.json trae latitud/longitud, arrancamos el mapa de satélites solos
// y ocultamos el botón/formulario manual — no hacen falta si ya está
// configurado. Si NO hay config.json (o no trae ubicación), se deja el botón
// visible como respaldo — importante para quien clone el repo sin configurar
// nada, ya que la geolocalización automática del navegador no funciona por
// HTTP (ver fix anterior de isSecureContext).
(async function initFromConfig() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    if (cfg.latitude != null && cfg.longitude != null) {
      document.getElementById('sky-hint').textContent = 'Ubicación tomada de config.json.';
      document.getElementById('btn-geo').style.display = 'none';
      document.getElementById('manual-geo').style.display = 'none';
      startSkyTracking(cfg.latitude, cfg.longitude);
    }
  } catch (e) { /* sin config.json - se usa el flujo manual normal */ }
})();

// ── Historial: rango + tabla + gráfico + export ─────────────────────────────
function histRangeSeconds() {
  const hours = parseFloat(document.getElementById('hist-range').value);
  return hours * 3600;
}

function fmtFecha(ts) {
  return new Date(ts * 1000).toLocaleString('es-AR', { dateStyle: 'short', timeStyle: 'short' });
}

async function loadHistory() {
  const now = Math.floor(Date.now() / 1000);
  const from = now - histRangeSeconds();
  let rows = [];
  try {
    const res = await fetch(`/api/history?from=${from}&to=${now}`);
    const data = await res.json();
    rows = data.rows || [];
  } catch (e) {
    document.getElementById('hist-count').textContent = 'No se pudo cargar el historial.';
    return;
  }

  document.getElementById('hist-count').textContent = `${rows.length} puntos`;

  const tbody = document.getElementById('hist-table-body');
  // Mas recientes primero en la tabla, hasta 500 filas para no colgar el navegador
  tbody.innerHTML = rows.slice(-500).reverse().map(r => `
    <tr style="border-top:1px solid var(--line)">
      <td style="padding:5px 10px">${fmtFecha(r.ts)}</td>
      <td style="padding:5px 10px;text-align:right">${fmtMs(r.latency_ms)}</td>
      <td style="padding:5px 10px;text-align:right">${fmtMbps(r.down_bps)}</td>
      <td style="padding:5px 10px;text-align:right">${fmtMbps(r.up_bps)}</td>
      <td style="padding:5px 10px;text-align:right">${fmtPct(r.drop_rate)}</td>
      <td style="padding:5px 10px;text-align:right">${fmtPct(r.obstructed_fraction)}</td>
      <td style="padding:5px 10px">${r.state || '—'}</td>
      <td style="padding:5px 10px;text-align:right">${r.cloud_cover != null ? r.cloud_cover.toFixed(0) + '%' : '—'}</td>
      <td style="padding:5px 10px;text-align:right">${r.precipitation != null ? r.precipitation.toFixed(1) + ' mm' : '—'}</td>
    </tr>
  `).join('') || '<tr><td colspan="9" style="padding:10px;color:var(--muted)">Sin datos en este rango todavía.</td></tr>';

  drawHistChart(rows);
}

function drawHistChart(rows) {
  const canvas = document.getElementById('hist-chart');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const pts = rows.filter(r => r.latency_ms != null);
  if (pts.length < 2) {
    ctx.fillStyle = '#9aa3af';
    ctx.font = '12px system-ui';
    ctx.fillText('Sin suficientes datos todavía para graficar.', 10, H / 2);
    return;
  }

  function series(key, color) {
    const vals = pts.map(p => p[key]).filter(v => v != null);
    if (!vals.length) return;
    const min = Math.min(...vals), max = Math.max(...vals) || 1;
    const range = (max - min) || 1;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = (i / (pts.length - 1)) * (W - 20) + 10;
      const norm = p[key] == null ? 0 : (p[key] - min) / range;
      const y = H - 15 - norm * (H - 30);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  series('latency_ms', '#4a9eff');
  series('down_bps', '#4ac97a');
  series('obstructed_fraction', '#e0a934');
  series('cloud_cover', '#5ecfe0');
  series('precipitation', '#c17ae0');

  ctx.font = '11px system-ui';
  ctx.fillStyle = '#4a9eff'; ctx.fillText('latencia', 10, 14);
  ctx.fillStyle = '#4ac97a'; ctx.fillText('bajada', 70, 14);
  ctx.fillStyle = '#e0a934'; ctx.fillText('obstrucción', 120, 14);
  ctx.fillStyle = '#5ecfe0'; ctx.fillText('nubosidad', 210, 14);
  ctx.fillStyle = '#c17ae0'; ctx.fillText('precipitación', 280, 14);
}

document.getElementById('btn-hist-refresh').addEventListener('click', loadHistory);
document.getElementById('hist-range').addEventListener('change', loadHistory);
document.getElementById('btn-hist-export').addEventListener('click', () => {
  const now = Math.floor(Date.now() / 1000);
  const from = now - histRangeSeconds();
  window.location.href = `/api/history/export?from=${from}&to=${now}`;
});

loadHistory();
setInterval(loadHistory, 60000);
