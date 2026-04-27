/* ── Gauge math ───────────────────────────────────────────────────────────── */

// Coordinate system: 0° = 12 o'clock, increasing clockwise
function polarToCartesian(cx, cy, r, deg) {
  const rad = (deg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

// Clockwise arc from startDeg, spanning pct × 270°
// Background arc: arcPath(100, 100, 72, 225, 1.0) → full 270° sweep
function arcPath(cx, cy, r, startDeg, pct) {
  pct = Math.max(0, Math.min(1, pct));
  if (pct <= 0) return '';

  const span = pct * 270;
  const endDeg = startDeg + span;
  const [sx, sy] = polarToCartesian(cx, cy, r, startDeg);
  const [ex, ey] = polarToCartesian(cx, cy, r, endDeg);
  const large = span > 180 ? 1 : 0;

  // Clamp to avoid SVG degenerate arc when pct ≈ 1
  if (pct >= 0.9999) {
    // Two-segment arc to avoid start === end
    const [mx, my] = polarToCartesian(cx, cy, r, startDeg + 135);
    return `M ${sx.toFixed(2)} ${sy.toFixed(2)}` +
           ` A ${r} ${r} 0 0 1 ${mx.toFixed(2)} ${my.toFixed(2)}` +
           ` A ${r} ${r} 0 1 1 ${ex.toFixed(2)} ${ey.toFixed(2)}`;
  }

  return `M ${sx.toFixed(2)} ${sy.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${ex.toFixed(2)} ${ey.toFixed(2)}`;
}

function setGauge(fillId, pct) {
  document.getElementById(fillId).setAttribute('d', arcPath(100, 100, 72, 225, pct));
}

/* ── Map ──────────────────────────────────────────────────────────────────── */

let map, carMarker, stationMarker, restStopMarker, destMarker;

function initMap() {
  map = L.map('map', {
    center: [34.0268, -118.3964],
    zoom: 12,
    zoomControl: true,
    attributionControl: false,
  });

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 19,
  }).addTo(map);

  carMarker     = makeMarker('map-car',      [34.0268, -118.3964]).addTo(map);
  stationMarker = makeMarker('map-station',  [34.0268, -118.3964]);
  restStopMarker = makeMarker('map-rest-stop', [34.0268, -118.3964]);
  destMarker    = makeMarker('map-dest',     [34.0268, -118.3964]);
}

function makeMarker(className, latlng) {
  return L.marker(latlng, {
    icon: L.divIcon({
      className: '',
      html: `<div class="${className}"></div>`,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    }),
  });
}

function setMarker(marker, lat, lng, show) {
  if (show && lat != null && lng != null) {
    marker.setLatLng([lat, lng]);
    if (!map.hasLayer(marker)) marker.addTo(map);
  } else {
    if (map.hasLayer(marker)) map.removeLayer(marker);
  }
}

/* ── Signal handler ───────────────────────────────────────────────────────── */

function onSignal(s) {
  // Speed gauge
  setGauge('speed-fill', s.speed_kmh / 200);
  document.getElementById('speed-val').textContent = Math.round(s.speed_kmh);

  // Fuel gauge + colour
  const fuelPct = s.fuel_percent / 100;
  setGauge('fuel-fill', fuelPct);
  const fuelEl = document.getElementById('fuel-fill');
  const fuelUnit = document.getElementById('fuel-unit');
  let fuelColor = '#4ade80';
  if (s.fuel_percent < 20) fuelColor = '#ef4444';
  else if (s.fuel_percent < 35) fuelColor = '#f59e0b';
  fuelEl.style.stroke = fuelColor;
  fuelUnit.style.fill = fuelColor;
  document.getElementById('fuel-val').textContent = Math.round(s.fuel_percent);

  // Center info
  document.getElementById('time-val').textContent = s.current_time;
  document.getElementById('location-val').textContent = s.location_label;
  document.getElementById('range-val').textContent = Math.round(s.range_km);
  document.getElementById('temp-val').textContent = Math.round(s.cabin_temp_c);
  document.getElementById('outside-val').textContent = Math.round(s.outside_temp_c);

  // Cabin status
  setStatusVal('windows-val', s.windows_open ? 'OPEN' : 'closed', s.windows_open);
  setStatusVal('sunroof-val', s.sunroof_open ? 'OPEN' : 'closed', s.sunroof_open);
  setStatusVal('ac-val', s.ac_on ? 'ON' : 'off', s.ac_on);

  const rainRow = document.getElementById('rain-row');
  if (s.rain_in_minutes != null) {
    rainRow.style.display = 'flex';
    document.getElementById('rain-val').textContent = `${s.rain_in_minutes} min`;
  } else {
    rainRow.style.display = 'none';
  }

  // Schedule block
  const schedBlock = document.getElementById('schedule-block');
  if (s.next_meeting_title) {
    schedBlock.style.display = 'block';
    document.getElementById('meeting-title').textContent = s.next_meeting_title;
    document.getElementById('meeting-time').textContent = s.next_meeting_time || '';
    document.getElementById('meeting-loc').textContent = s.next_meeting_location || '';

    const trafficRow = document.getElementById('traffic-row');
    const travelRow  = document.getElementById('travel-row');
    if (s.traffic_delay_minutes > 0) {
      trafficRow.style.display = 'flex';
      document.getElementById('traffic-val').textContent = `+${s.traffic_delay_minutes} min`;
    } else {
      trafficRow.style.display = 'none';
    }
    if (s.normal_travel_minutes != null) {
      const total = s.normal_travel_minutes + (s.traffic_delay_minutes || 0);
      travelRow.style.display = 'flex';
      document.getElementById('travel-val').textContent = `${total} min today`;
    } else {
      travelRow.style.display = 'none';
    }
  } else {
    schedBlock.style.display = 'none';
  }

  // Route
  document.getElementById('station-name').textContent = s.next_gas_station_name;
  document.getElementById('station-km').textContent   = `${Math.round(s.next_gas_station_km)} km`;

  // Map
  if (map) {
    carMarker.setLatLng([s.lat, s.lng]);
    map.panTo([s.lat, s.lng], { animate: true, duration: 0.6 });

    setMarker(stationMarker, s.next_gas_station_lat, s.next_gas_station_lng, true);
    setMarker(restStopMarker, s.next_rest_stop_lat, s.next_rest_stop_lng,
              s.next_rest_stop_km != null);
    setMarker(destMarker, s.destination_lat, s.destination_lng,
              s.destination != null);
  }
}

function setStatusVal(id, text, warn) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'status-val' + (warn ? ' warn' : '');
}

/* ── WebSocket ────────────────────────────────────────────────────────────── */

let ws;

function connect() {
  ws = new WebSocket('ws://localhost:8000/ws');

  ws.onopen = () => setStatus('connected');
  ws.onclose = () => {
    setStatus('disconnected');
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'signal') onSignal(msg.data);
  };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function setStatus(state) {
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  dot.className = 'status-dot ' + state;
  label.textContent = state;
}

/* ── Init ─────────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initMap();
  connect();

  document.querySelectorAll('[data-scenario]').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.scenario;
      send({ type: 'play', scenario: name });
      document.querySelectorAll('[data-scenario]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('scenario-label').textContent = name.replace('_', ' ');
    });
  });
});
