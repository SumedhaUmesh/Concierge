/* ── Gauge math ───────────────────────────────────────────────────────────── */

function polarToCartesian(cx, cy, r, deg) {
  const rad = (deg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function arcPath(cx, cy, r, startDeg, pct) {
  pct = Math.max(0, Math.min(1, pct));
  if (pct <= 0) return '';

  const span = pct * 270;
  const endDeg = startDeg + span;
  const [sx, sy] = polarToCartesian(cx, cy, r, startDeg);
  const [ex, ey] = polarToCartesian(cx, cy, r, endDeg);
  const large = span > 180 ? 1 : 0;

  if (pct >= 0.9999) {
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

let map, carMarker, stationMarker, restStopMarker, destMarker, enrichedMarker;
let routePolyline = null;
let _lastRouteDest = null;

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

  carMarker      = makeMarker('map-car',      [34.0268, -118.3964]).addTo(map);
  stationMarker  = makeMarker('map-station',  [34.0268, -118.3964]);
  restStopMarker = makeMarker('map-rest-stop',[34.0268, -118.3964]);
  destMarker     = makeMarker('map-dest',     [34.0268, -118.3964]);
  enrichedMarker = makeMarker('map-enriched', [34.0268, -118.3964]);
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

async function drawRoute(fromLat, fromLng, toLat, toLng) {
  try {
    const res = await fetch(
      `/route?from_lat=${fromLat}&from_lng=${fromLng}&to_lat=${toLat}&to_lng=${toLng}`
    );
    if (!res.ok) return;
    const data = await res.json();
    const coords = data.points || [];
    if (!Array.isArray(coords) || coords.length < 2) return;
    if (routePolyline) { map.removeLayer(routePolyline); routePolyline = null; }
    routePolyline = L.polyline(coords, {
      color: '#60a5fa',
      weight: 3,
      opacity: 0.75,
      dashArray: null,
    }).addTo(map);
  } catch (_) {}
}

/* ── Signal handler ───────────────────────────────────────────────────────── */

function onSignal(s) {
  setGauge('speed-fill', s.speed_kmh / 200);
  document.getElementById('speed-val').textContent = Math.round(s.speed_kmh);

  const fuelPct = s.fuel_percent / 100;
  setGauge('fuel-fill', fuelPct);
  const fuelEl   = document.getElementById('fuel-fill');
  const fuelUnit = document.getElementById('fuel-unit');
  let fuelColor = '#4ade80';
  if (s.fuel_percent < 20) fuelColor = '#ef4444';
  else if (s.fuel_percent < 35) fuelColor = '#f59e0b';
  fuelEl.style.stroke = fuelColor;
  fuelUnit.style.fill = fuelColor;
  document.getElementById('fuel-val').textContent = Math.round(s.fuel_percent);

  document.getElementById('time-val').textContent     = s.current_time;
  document.getElementById('location-val').textContent = s.location_label;
  document.getElementById('range-val').textContent    = Math.round(s.range_km);
  document.getElementById('temp-val').textContent     = Math.round(s.cabin_temp_c);
  document.getElementById('outside-val').textContent  = Math.round(s.outside_temp_c);

  setStatusVal('windows-val', s.windows_open ? 'OPEN' : 'closed', s.windows_open);
  setStatusVal('sunroof-val', s.sunroof_open ? 'OPEN' : 'closed', s.sunroof_open);
  setStatusVal('ac-val',      s.ac_on ? 'ON'   : 'off',   s.ac_on);

  const rainRow = document.getElementById('rain-row');
  if (s.rain_in_minutes != null) {
    rainRow.style.display = 'flex';
    document.getElementById('rain-val').textContent = `${s.rain_in_minutes} min`;
  } else {
    rainRow.style.display = 'none';
  }

  const schedBlock = document.getElementById('schedule-block');
  if (s.next_meeting_title) {
    schedBlock.style.display = 'block';
    document.getElementById('meeting-title').textContent = s.next_meeting_title;
    document.getElementById('meeting-time').textContent  = s.next_meeting_time || '';
    document.getElementById('meeting-loc').textContent   = s.next_meeting_location || '';

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

  document.getElementById('station-name').textContent = s.next_gas_station_name;
  document.getElementById('station-km').textContent   = `${Math.round(s.next_gas_station_km)} km`;

  // Cognitive Driver Model bars
  if (s.fatigue_index !== undefined) {
    setDriverBar('ds-fatigue', s.fatigue_index);
    setDriverBar('ds-load',    s.cognitive_load);
    setDriverBar('ds-stress',  s.stress_index);
    const risk = (s.driver_risk || 'low').toUpperCase();
    const riskEl = document.getElementById('ds-risk');
    riskEl.textContent = risk;
    riskEl.className = 'ds-risk risk-' + (s.driver_risk || 'low');
  }

  if (map) {
    carMarker.setLatLng([s.lat, s.lng]);
    map.panTo([s.lat, s.lng], { animate: true, duration: 0.6 });

    setMarker(stationMarker, s.next_gas_station_lat, s.next_gas_station_lng, true);
    setMarker(restStopMarker, s.next_rest_stop_lat, s.next_rest_stop_lng,
              s.next_rest_stop_km != null);
    setMarker(destMarker, s.destination_lat, s.destination_lng,
              s.destination != null);

    // Draw / clear OSRM route when destination changes
    const destKey = (s.destination_lat != null && s.destination_lng != null)
      ? `${s.destination_lat.toFixed(4)},${s.destination_lng.toFixed(4)}` : null;
    if (destKey !== _lastRouteDest) {
      _lastRouteDest = destKey;
      if (destKey) {
        drawRoute(s.lat, s.lng, s.destination_lat, s.destination_lng);
      } else {
        if (routePolyline) { map.removeLayer(routePolyline); routePolyline = null; }
      }
    }
  }
}

function setStatusVal(id, text, warn) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'status-val' + (warn ? ' warn' : '');
}

function setDriverBar(id, value) {
  const fill = document.getElementById(id);
  if (!fill) return;
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  fill.style.width = pct + '%';
  fill.className = 'ds-fill' + (pct > 65 ? ' ds-fill-high' : pct > 35 ? ' ds-fill-mid' : '');
}

/* ── Suggestion handler ───────────────────────────────────────────────────── */

const TYPE_ICONS = {
  range: '⛽',
  meal: '🍽',
  cabin: '🌧',
  schedule: '📅',
  music: '♪',
};

function onSuggestion(s) {
  setAgentDot('active');
  document.getElementById('agent-status-text').textContent = 'Suggestion';

  const card = document.getElementById('suggestion-card');
  const idle = document.getElementById('agent-idle');
  card.style.display = 'block';
  idle.style.display = 'flex';

  // Remove urgency classes
  card.className = 'suggestion-card';
  if (s.urgency >= 4) card.classList.add(`urgency-${s.urgency}`);

  document.getElementById('sug-icon').textContent    = TYPE_ICONS[s.type] || '●';
  document.getElementById('sug-type').textContent    = s.type.toUpperCase();
  document.getElementById('sug-urgency').textContent = '●'.repeat(s.urgency);
  document.getElementById('sug-headline').textContent = s.headline;
  document.getElementById('sug-detail').textContent   = s.detail;

  const actionRow = document.getElementById('sug-action-row');
  const actionBtn = document.getElementById('sug-action-btn');

  if (s.enriched_action) {
    actionRow.style.display = 'block';
    actionBtn.textContent = s.enriched_action.label || 'Act';
    actionBtn.onclick = () => handleAction(s.enriched_action);

    // Pin enriched POI on map
    if (s.enriched_action.lat && s.enriched_action.lng) {
      setMarker(enrichedMarker, s.enriched_action.lat, s.enriched_action.lng, true);
    }
  } else {
    actionRow.style.display = 'none';
  }
}

function handleAction(action) {
  if (action.type === 'navigate') {
    const url = `https://maps.google.com/?q=${action.lat},${action.lng}`;
    window.open(url, '_blank');
    send({ type: 'user_accept' });
  } else if (action.type === 'cabin_action') {
    send({ type: 'user_accept', action: action.action });
  }
}

function dismissSuggestion() {
  document.getElementById('suggestion-card').style.display = 'none';
  setAgentDot('idle');
  document.getElementById('agent-status-text').textContent = 'Listening…';
  setMarker(enrichedMarker, 0, 0, false);
  send({ type: 'user_dismiss' });
}

function setAgentDot(state) {
  const dot = document.getElementById('agent-dot');
  dot.className = 'agent-dot ' + state;
}

/* ── Track synthesizer ────────────────────────────────────────────────────── */

const player = {
  ctx: null,
  oscs: [],
  masterGain: null,

  play(track) {
    this.stop();
    this.ctx = new AudioContext();

    this.masterGain = this.ctx.createGain();
    this.masterGain.gain.setValueAtTime(0, this.ctx.currentTime);
    this.masterGain.gain.linearRampToValueAtTime(0.18, this.ctx.currentTime + 1.5);
    this.masterGain.connect(this.ctx.destination);

    const energy  = track.energy;          // 1–10
    const bps     = track.tempo / 60;      // beats per second
    const baseHz  = 55 + energy * 14;      // 69 Hz (calm) → 195 Hz (intense)
    const wave    = energy <= 3 ? 'sine' : energy <= 6 ? 'triangle' : 'sawtooth';

    // Root pad
    const root = this.ctx.createOscillator();
    root.type = wave;
    root.frequency.value = baseHz;
    const rootGain = this.ctx.createGain();
    rootGain.gain.value = 0.45;
    root.connect(rootGain).connect(this.masterGain);
    root.start();

    // Fifth harmonic (perfect 5th = ×1.5)
    const fifth = this.ctx.createOscillator();
    fifth.type = 'sine';
    fifth.frequency.value = baseHz * 1.5;
    const fifthGain = this.ctx.createGain();
    fifthGain.gain.value = 0.2;
    fifth.connect(fifthGain).connect(this.masterGain);
    fifth.start();

    // Octave (subtle shimmer on high energy)
    const oct = this.ctx.createOscillator();
    oct.type = 'sine';
    oct.frequency.value = baseHz * 2;
    const octGain = this.ctx.createGain();
    octGain.gain.value = energy > 5 ? 0.12 : 0.04;
    oct.connect(octGain).connect(this.masterGain);
    oct.start();

    // LFO — tempo-locked tremolo
    const lfo = this.ctx.createOscillator();
    lfo.frequency.value = bps * (energy > 6 ? 1 : 0.25);
    const lfoGain = this.ctx.createGain();
    lfoGain.gain.value = 0.04;
    lfo.connect(lfoGain).connect(this.masterGain.gain);
    lfo.start();

    // Lowpass filter — roll off harshness on calm tracks
    const filter = this.ctx.createBiquadFilter();
    filter.type = 'lowpass';
    filter.frequency.value = 300 + energy * 120;
    root.disconnect(rootGain);
    root.connect(filter);
    filter.connect(rootGain);

    this.oscs = [root, fifth, oct, lfo];
  },

  stop() {
    if (!this.ctx) return;
    const g = this.masterGain;
    const c = this.ctx;
    const os = this.oscs;
    g.gain.cancelScheduledValues(c.currentTime);
    g.gain.linearRampToValueAtTime(0, c.currentTime + 0.8);
    setTimeout(() => {
      os.forEach(o => { try { o.stop(); } catch (_) {} });
      c.close();
    }, 900);
    this.ctx = null;
    this.oscs = [];
    document.getElementById('now-playing').style.display = 'none';
  },
};

let _previewAudio = null;

function _setPauseBtn(icon) {
  const btn = document.getElementById('pause-btn');
  if (btn) btn.textContent = icon;
}

async function playTrack(track) {
  // Stop any previous audio
  if (_previewAudio) { _previewAudio.pause(); _previewAudio = null; }
  player.stop();

  // Start synth immediately as placeholder
  player.play(track);
  _setPauseBtn('⏸');

  const bar = document.getElementById('now-playing');
  bar.style.display = 'flex';
  document.getElementById('now-playing-title').textContent = `${track.title} · ${track.genres[0]} — loading…`;

  // Search iTunes for a matching preview (no API key needed)
  const query = encodeURIComponent(`${track.genres[0]} ${track.mood[0]}`);
  try {
    const res = await fetch(
      `https://itunes.apple.com/search?term=${query}&media=music&entity=song&limit=5`
    );
    const data = await res.json();
    const hit = data.results.find(r => r.previewUrl);
    if (hit) {
      player.stop();  // fade out synth
      _previewAudio = new Audio(hit.previewUrl);
      _previewAudio.volume = 0.7;
      _previewAudio.play();
      _setPauseBtn('⏸');
      document.getElementById('now-playing-title').textContent =
        `${hit.trackName} — ${hit.artistName}`;
      _previewAudio.onended = () => {
        document.getElementById('now-playing').style.display = 'none';
        _setPauseBtn('⏸');
        _previewAudio = null;
      };
    } else {
      document.getElementById('now-playing-title').textContent =
        `${track.title} · ${track.genres[0]}`;
    }
  } catch (_) {
    // iTunes unreachable — synth keeps playing
    document.getElementById('now-playing-title').textContent =
      `${track.title} · ${track.genres[0]}`;
  }
}

function togglePause() {
  if (_previewAudio) {
    if (_previewAudio.paused) {
      _previewAudio.play();
      _setPauseBtn('⏸');
    } else {
      _previewAudio.pause();
      _setPauseBtn('▶');
    }
  } else {
    // Synth only — no pause support, treat as stop
    stopTrack();
  }
}

function stopTrack() {
  if (_previewAudio) { _previewAudio.pause(); _previewAudio = null; }
  player.stop();
  _setPauseBtn('⏸');
  document.getElementById('now-playing').style.display = 'none';
}

/* ── Music handler ────────────────────────────────────────────────────────── */

function onTranscript(data) {
  const row = document.getElementById('transcript-row');
  const txt = document.getElementById('transcript-text');
  row.style.display = 'block';
  txt.textContent = `"${data.text}"`;
  setAgentDot('active');
  document.getElementById('agent-status-text').textContent = 'Heard';
  setTimeout(() => { row.style.display = 'none'; }, 5000);
}

function onMusicResults(data) {
  const section = document.getElementById('music-section');
  const list    = document.getElementById('music-tracks');
  section.style.display = 'block';
  list.innerHTML = '';

  // If the query looks like a specific song ("X by Y"), offer a direct iTunes search first
  if (/\bby\b/i.test(data.query)) {
    const direct = document.createElement('div');
    direct.className = 'music-track music-direct';
    direct.title = 'Search iTunes for this exact song';
    direct.innerHTML = `
      <div class="music-track-play">▶</div>
      <div>
        <div class="music-track-title">"${data.query}"</div>
        <div class="music-track-meta">iTunes preview</div>
      </div>
    `;
    direct.addEventListener('click', () => {
      document.querySelectorAll('.music-track').forEach(el => el.classList.remove('playing'));
      direct.classList.add('playing');
      direct.querySelector('.music-track-play').textContent = '♪';
      playITunesDirect(data.query);
    });
    list.appendChild(direct);
  }

  data.tracks.forEach(t => {
    const div = document.createElement('div');
    div.className = 'music-track';
    div.title = 'Click to play';
    div.innerHTML = `
      <div class="music-track-play">▶</div>
      <div>
        <div class="music-track-title">${t.title}</div>
        <div class="music-track-meta">${t.genres.join(', ')} · energy ${t.energy}/10</div>
      </div>
    `;
    div.addEventListener('click', () => {
      document.querySelectorAll('.music-track').forEach(el => el.classList.remove('playing'));
      div.classList.add('playing');
      div.querySelector('.music-track-play').textContent = '♪';
      playTrack(t);
    });
    list.appendChild(div);
  });
}

async function playITunesDirect(query) {
  if (_previewAudio) { _previewAudio.pause(); _previewAudio = null; }
  player.stop();
  _setPauseBtn('⏸');

  const bar = document.getElementById('now-playing');
  bar.style.display = 'flex';
  document.getElementById('now-playing-title').textContent = `Searching: "${query}"…`;

  try {
    const res = await fetch(
      `https://itunes.apple.com/search?term=${encodeURIComponent(query)}&media=music&entity=song&limit=5`
    );
    const data = await res.json();
    const hit = data.results.find(r => r.previewUrl);
    if (hit) {
      _previewAudio = new Audio(hit.previewUrl);
      _previewAudio.volume = 0.7;
      _previewAudio.play();
      _setPauseBtn('⏸');
      document.getElementById('now-playing-title').textContent =
        `${hit.trackName} — ${hit.artistName}`;
      _previewAudio.onended = () => {
        document.getElementById('now-playing').style.display = 'none';
        _setPauseBtn('⏸');
        _previewAudio = null;
      };
    } else {
      document.getElementById('now-playing-title').textContent = 'No preview found';
    }
  } catch (_) {
    document.getElementById('now-playing-title').textContent = 'iTunes unavailable';
  }
}

function onMealOptions(data) {
  const section = document.getElementById('music-section');
  const list    = document.getElementById('music-tracks');
  section.style.display = 'block';
  list.innerHTML = '';

  // Question header
  const hdr = document.createElement('div');
  hdr.className = 'section-label';
  hdr.style.marginBottom = '6px';
  hdr.textContent = '🍽 NEARBY RESTAURANTS';
  list.appendChild(hdr);

  data.pois.forEach(p => {
    const div = document.createElement('div');
    div.className = 'music-track';
    div.innerHTML = `
      <div class="music-track-play">▶</div>
      <div>
        <div class="music-track-title">${p.name}</div>
        <div class="music-track-meta">${p.cuisine || 'restaurant'} · ${p.distance_km} km</div>
      </div>
    `;
    div.addEventListener('click', () => {
      document.querySelectorAll('.music-track').forEach(el => el.classList.remove('playing'));
      div.classList.add('playing');
      if (p.lat && p.lng) {
        const url = `https://maps.google.com/?q=${p.lat},${p.lng}`;
        window.open(url, '_blank');
      }
      send({ type: 'user_accept' });
    });
    list.appendChild(div);
  });
}

/* ── Push-to-talk recorder ────────────────────────────────────────────────── */

let audioCtx = null;
let pttStream = null;
let pttProcessor = null;
let pttSamples = [];
let pttRecording = false;
let _muted = false;

function encodeWAV(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buf);
  const write = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); };
  write(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, 'WAVE');
  write(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    view.setInt16(44 + i * 2, Math.max(-32768, Math.min(32767, samples[i] * 32768)), true);
  }
  return buf;
}

async function startRecording() {
  pttSamples = [];
  pttRecording = true;
  _setBtnState('recording');

  try {
    const md = navigator.mediaDevices;
    if (!md) throw new Error('mediaDevices unavailable — open http://localhost:8000');
    pttStream = await md.getUserMedia({ audio: true });
    audioCtx = new AudioContext({ sampleRate: 16000 });
    const source = audioCtx.createMediaStreamSource(pttStream);
    pttProcessor = audioCtx.createScriptProcessor(4096, 1, 1);
    pttProcessor.onaudioprocess = (e) => {
      if (!pttRecording) return;
      const input = e.inputBuffer.getChannelData(0);
      pttSamples.push(...input);
    };
    source.connect(pttProcessor);
    pttProcessor.connect(audioCtx.destination);
  } catch (err) {
    console.error('Mic error:', err.message);
    document.getElementById('agent-status-text').textContent = err.message.includes('mediaDevices') ? 'Use localhost:8000' : 'Mic denied — check browser settings';
    _setBtnState('idle');
  }
}

function stopRecording() {
  if (!pttRecording) return;
  pttRecording = false;

  if (pttProcessor) { pttProcessor.disconnect(); pttProcessor = null; }
  if (pttStream)    { pttStream.getTracks().forEach(t => t.stop()); pttStream = null; }
  if (audioCtx)     { audioCtx.close(); audioCtx = null; }

  if (pttSamples.length < 16000) {  // < 1 s — skip
    _setBtnState(_fromWake ? 'armed' : 'idle');
    if (_fromWake) _startWakeListener();
    return;
  }

  const wav = encodeWAV(new Float32Array(pttSamples), 16000);
  const bytes = new Uint8Array(wav);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  const b64 = btoa(binary);
  send({ type: 'voice_input', audio: b64 });

  setAgentDot('thinking');
  document.getElementById('agent-status-text').textContent = 'Transcribing…';

  // After wake-word triggered recording, re-arm automatically
  if (_fromWake) {
    setTimeout(() => { _setBtnState('armed'); _startWakeListener(); }, 1500);
  } else {
    _setBtnState('idle');
  }
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
    if      (msg.type === 'signal')        onSignal(msg.data);
    else if (msg.type === 'suggestion')    onSuggestion(msg.data);
    else if (msg.type === 'music_results') onMusicResults(msg.data);
    else if (msg.type === 'meal_options')  onMealOptions(msg.data);
    else if (msg.type === 'transcript')    onTranscript(msg.data);
    else if (msg.type === 'user_accept')   { document.getElementById('suggestion-card').style.display = 'none'; setAgentDot(''); }
    else if (msg.type === 'user_dismiss')  dismissSuggestion();
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

/* ── Real GPS ─────────────────────────────────────────────────────────────── */

let _gpsWatchId   = null;
let _gpsActive    = false;
let _lastGeocodeLat = null;
let _lastGeocodeLng = null;

async function _reverseGeocode(lat, lng) {
  // Only re-geocode when position changes by >300 m
  if (_lastGeocodeLat !== null) {
    const dlat = lat - _lastGeocodeLat, dlng = lng - _lastGeocodeLng;
    if (Math.sqrt(dlat*dlat + dlng*dlng) < 0.003) return null;
  }
  _lastGeocodeLat = lat; _lastGeocodeLng = lng;
  try {
    const res = await fetch(`/geocode/reverse?lat=${lat}&lng=${lng}`);
    const d = await res.json();
    return d.label || null;
  } catch (_) { return null; }
}

async function _onGpsPosition(pos) {
  const { latitude: lat, longitude: lng, accuracy } = pos.coords;
  const label = await _reverseGeocode(lat, lng);
  send({ type: 'gps_update', lat, lng, label });
  // Update GPS indicator
  const ind = document.getElementById('gps-indicator');
  if (ind) ind.title = `GPS ±${Math.round(accuracy)} m`;
}

function startGPS() {
  if (!navigator.geolocation) {
    alert('Geolocation not available in this browser.');
    return;
  }
  _gpsActive = true;
  _gpsWatchId = navigator.geolocation.watchPosition(
    _onGpsPosition,
    (err) => console.warn('GPS error:', err.message),
    { enableHighAccuracy: true, maximumAge: 4000, timeout: 10000 }
  );
  const btn = document.getElementById('gps-btn');
  if (btn) { btn.classList.add('on'); btn.textContent = '📍 GPS live'; }
}

function stopGPS() {
  if (_gpsWatchId !== null) navigator.geolocation.clearWatch(_gpsWatchId);
  _gpsWatchId = null; _gpsActive = false;
  const btn = document.getElementById('gps-btn');
  if (btn) { btn.classList.remove('on'); btn.textContent = '📍 GPS off'; }
}

function toggleGPS() {
  _gpsActive ? stopGPS() : startGPS();
}

/* ── Voice button — three states: idle → armed → recording ───────────────── */
//
//  idle     → tap → armed    (wake word listener starts, button pulses green)
//  armed    → "Hey Concierge" → recording  (auto-stop 5 s, then re-arms)
//  armed    → tap → recording (skip wake word; goes back to idle after)
//  recording → tap → stop

let _btnState  = 'idle';
let _wakeRecog = null;
let _fromWake  = false;
const WAKE_WORDS = ['concierge', 'hey concierge', 'hey car'];

function _setBtnState(state) {
  _btnState = state;
  const btn    = document.getElementById('ptt-btn');
  const icon   = document.getElementById('ptt-icon');
  const status = document.getElementById('agent-status-text');
  btn.classList.remove('recording', 'armed');
  if (state === 'idle') {
    icon.textContent  = '🎙';
    btn.title         = 'Tap to speak';
    status.textContent = 'Listening…';
  } else if (state === 'armed') {
    icon.textContent  = '👂';
    btn.classList.add('armed');
    btn.title         = 'Say "Hey Concierge" or tap to record';
    status.textContent = 'Say "Hey Concierge"…';
  } else if (state === 'recording') {
    icon.textContent  = '⏹';
    btn.classList.add('recording');
    btn.title         = 'Tap to stop';
    status.textContent = 'Recording…';
  }
}

function _startWakeListener() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;
  _wakeRecog = new SR();
  _wakeRecog.continuous     = true;
  _wakeRecog.interimResults = true;
  _wakeRecog.lang = 'en-US';
  _wakeRecog.onresult = (e) => {
    if (_btnState !== 'armed') return;
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript.toLowerCase().trim();
      if (WAKE_WORDS.some(w => t.includes(w))) {
        _stopWakeListener();
        _fromWake = true;
        _setBtnState('recording');
        document.getElementById('agent-status-text').textContent = 'Wake word — speak now…';
        setTimeout(() => startRecording(), 250);
        setTimeout(() => { if (pttRecording) stopRecording(); }, 5000);
        return;
      }
    }
  };
  _wakeRecog.onend  = () => { if (_btnState === 'armed') _wakeRecog.start(); };
  _wakeRecog.onerror = (e) => { if (e.error !== 'no-speech') console.warn('wake:', e.error); };
  try { _wakeRecog.start(); } catch (_) {}
}

function _stopWakeListener() {
  if (_wakeRecog) { try { _wakeRecog.stop(); } catch (_) {} _wakeRecog = null; }
}

function handleVoiceBtn() {
  if (_btnState === 'idle') {
    _fromWake = false;
    _setBtnState('armed');
    _startWakeListener();
  } else if (_btnState === 'armed') {
    _stopWakeListener();
    _fromWake = false;
    _setBtnState('recording');
    startRecording();
  } else if (_btnState === 'recording') {
    stopRecording();
  }
}

/* ── Init ─────────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initMap();
  connect();

document.getElementById('sug-dismiss-btn').addEventListener('click', dismissSuggestion);

  // Pre-warm mic permission so the first click works immediately
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    navigator.mediaDevices.getUserMedia({ audio: true })
      .then(s => s.getTracks().forEach(t => t.stop()))
      .catch(() => {});
  }

  document.getElementById('ptt-btn').addEventListener('click', handleVoiceBtn);
  const gpsBtn = document.getElementById('gps-btn');
  if (gpsBtn) gpsBtn.addEventListener('click', toggleGPS);

  // One-shot GPS fix on load to set real position without continuous tracking
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const { latitude: lat, longitude: lng } = pos.coords;
        const label = await _reverseGeocode(lat, lng);
        send({ type: 'gps_update', lat, lng, label });
      },
      () => {},  // silently ignore if denied
      { enableHighAccuracy: true, timeout: 8000 }
    );
  }

  // Mute toggle
  document.getElementById('mute-btn').addEventListener('click', () => {
    _muted = !_muted;
    const btn = document.getElementById('mute-btn');
    btn.textContent = _muted ? '🔇' : '🔊';
    btn.classList.toggle('muted', _muted);
    send({ type: 'mute', muted: _muted });
  });


});
