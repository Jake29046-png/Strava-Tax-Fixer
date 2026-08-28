// Strava Tax Fixer - web build
// Runs entirely client-side: Pyodide (Python compiled to WebAssembly) executes
// the exact same fit_engine.py / xml_engine.py logic used in the desktop app,
// via web_bridge.py as the JS<->Python glue. Nothing is ever uploaded anywhere.

let pyodide = null;
let webBridge = null;
let currentHandle = null;
let currentFilename = null;
let currentSummary = null;
let pendingWorkingHandle = null; // set by preview/save after a successful apply_edits

const $ = (id) => document.getElementById(id);

function log(msg) {
  const el = $('log');
  el.textContent += '\n' + msg;
  el.scrollTop = el.scrollHeight;
}

function setLoading(text) {
  $('loadingOverlay').style.display = 'flex';
  $('loadingText').textContent = text;
}
function hideLoading() {
  $('loadingOverlay').style.display = 'none';
}

async function boot() {
  try {
    setLoading('Loading Python runtime…');
    pyodide = await loadPyodide();

    setLoading('Loading activity engine…');
    const files = ['fit_engine.py', 'xml_engine.py', 'web_bridge.py'];
    for (const f of files) {
      const resp = await fetch(f);
      if (!resp.ok) throw new Error(`Failed to fetch ${f}: ${resp.status}`);
      const text = await resp.text();
      pyodide.FS.writeFile(f, text);
    }
    await pyodide.runPythonAsync(`
import sys
sys.path.insert(0, '.')
import web_bridge
`);
    webBridge = pyodide.pyimport('web_bridge');
    hideLoading();
    log('Ready. Everything runs on this device - no uploads.');
  } catch (err) {
    setLoading('Failed to start: ' + err.message + ' — try reloading.');
    console.error(err);
  }
}

// ------------------------------------------------------------ file loading

function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function base64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function handleFile(file) {
  const name = file.name;
  const ext = name.toLowerCase().slice(name.lastIndexOf('.'));
  if (!['.fit', '.tcx', '.gpx'].includes(ext)) {
    alert('Please choose a .fit, .tcx, or .gpx file.');
    return;
  }

  $('dropzoneText').textContent = 'Loading ' + name + '…';

  let content;
  if (ext === '.fit') {
    const buf = new Uint8Array(await file.arrayBuffer());
    content = bytesToBase64(buf);
  } else {
    content = await file.text();
  }

  const resultJson = webBridge.load(name, content);
  const result = JSON.parse(resultJson);

  if (!result.ok) {
    $('dropzoneText').textContent = 'Drag & drop a .FIT, .TCX, or .GPX file here<br>or tap to browse';
    alert('Could not read this file: ' + result.error);
    log('ERROR loading ' + name + ': ' + result.error);
    return;
  }

  currentHandle = result.handle;
  currentFilename = name;
  currentSummary = result.summary;

  $('dropzoneText').textContent = 'Loaded: ' + name;

  const s = result.summary;
  $('statDistance').textContent = s.distance_km.toFixed(2) + ' km';
  $('statStart').textContent = s.start_time ? s.start_time.slice(11, 16) : '—';
  $('statGain').textContent = s.elevation_gain_m != null ? Math.round(s.elevation_gain_m) + ' m' : '—';
  $('statHr').textContent = s.avg_heart_rate != null ? Math.round(s.avg_heart_rate) : '—';

  $('statusLine').textContent = `${name} (${s.format.toUpperCase()}) · ${s.num_laps} laps · ${s.num_records} records · start ${s.start_time || 'unknown'}`;

  populateFormFromSummary(s);

  $('saveBtn').disabled = false;
  $('previewBtn').disabled = false;
  $('resetBtn').disabled = false;

  log(`Loaded ${name}: ${s.distance_km.toFixed(2)} km, ${s.num_laps} laps, ${s.num_records} records.`);
}

function populateFormFromSummary(s) {
  $('f_distance').value = s.distance_km.toFixed(2);
  $('f_start').value = s.start_time || '';
  $('f_elevation').value = '0';
  $('f_hr').value = s.avg_heart_rate != null ? Math.round(s.avg_heart_rate) : '';
  $('f_duration').value = s.duration || '';
  $('f_cadence').value = s.avg_cadence != null ? Math.round(s.avg_cadence) : '';
  $('f_power').value = s.avg_power != null ? Math.round(s.avg_power) : '';
  $('f_calories').value = s.calories != null ? Math.round(s.calories) : '';
}

function collectEdits() {
  return {
    distance_km: $('f_distance').value,
    start_time: $('f_start').value,
    elevation_m: $('f_elevation').value,
    heart_rate: $('f_hr').value,
    duration: $('f_duration').value,
    cadence: $('f_cadence').value,
    power: $('f_power').value,
    calories: $('f_calories').value,
  };
}

function applyEdits() {
  if (!currentHandle) return null;
  const edits = collectEdits();
  const resultJson = webBridge.apply_edits(currentHandle, JSON.stringify(edits));
  return JSON.parse(resultJson);
}

// ---------------------------------------------------------------- actions

$('dropzone').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', (e) => {
  if (e.target.files.length) handleFile(e.target.files[0]);
});
['dragover', 'dragenter'].forEach(evt =>
  $('dropzone').addEventListener(evt, (e) => { e.preventDefault(); $('dropzone').classList.add('dragover'); })
);
['dragleave', 'drop'].forEach(evt =>
  $('dropzone').addEventListener(evt, (e) => { e.preventDefault(); $('dropzone').classList.remove('dragover'); })
);
$('dropzone').addEventListener('drop', (e) => {
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});

$('resetBtn').addEventListener('click', () => {
  if (currentSummary) populateFormFromSummary(currentSummary);
});

$('saveBtn').addEventListener('click', () => {
  if (!currentHandle) return;
  const result = applyEdits();
  if (!result.ok) {
    alert('Could not apply edits: ' + (result.error || 'unknown error'));
    log('ERROR: ' + (result.error || 'unknown error'));
    return;
  }
  if (result.no_changes) {
    alert('No values were changed.');
    return;
  }

  const finalJson = webBridge.finalize(result.working_handle, currentFilename);
  const final = JSON.parse(finalJson);
  if (!final.ok) {
    alert('Could not save: ' + final.error);
    log('ERROR saving: ' + final.error);
    return;
  }

  const bytes = base64ToBytes(final.content_b64);
  const blob = new Blob([bytes], { type: final.mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = final.filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 4000);

  log('Saved ' + final.filename);
  result.changes.forEach(c => log('  - ' + c));
});

let leafletMap = null;
let leafletLine = null;

$('previewBtn').addEventListener('click', () => {
  if (!currentHandle) return;
  const result = applyEdits();
  if (!result.ok) {
    alert('Could not build preview: ' + (result.error || 'unknown error'));
    return;
  }

  const profile = result.profile;
  $('previewChanges').textContent = result.no_changes
    ? 'Showing the current file (no pending edits).'
    : 'Showing your pending edits (not yet saved): ' + (result.changes || []).join('; ');

  $('previewModal').classList.add('open');

  // Route map (Leaflet + real OpenStreetMap tiles)
  const pts = [];
  for (let i = 0; i < profile.lat_deg.length; i++) {
    if (profile.lat_deg[i] != null && profile.lon_deg[i] != null) {
      pts.push([profile.lat_deg[i], profile.lon_deg[i]]);
    }
  }
  setTimeout(() => { // let the modal actually be visible before Leaflet measures it
    if (!leafletMap) {
      leafletMap = L.map('map', { zoomControl: false, attributionControl: true });
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: '© OpenStreetMap'
      }).addTo(leafletMap);
    }
    if (pts.length > 1) {
      if (leafletLine) leafletMap.removeLayer(leafletLine);
      leafletLine = L.polyline(pts, { color: '#F74F00', weight: 3 }).addTo(leafletMap);
      leafletMap.fitBounds(leafletLine.getBounds(), { padding: [16, 16] });
    }
    leafletMap.invalidateSize();
  }, 50);

  drawElevationChart(profile.distance_m, profile.altitude_m);
});

$('closePreviewBtn').addEventListener('click', () => {
  $('previewModal').classList.remove('open');
});

function drawElevationChart(distances, altitudes) {
  const canvas = $('elevCanvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const pts = [];
  for (let i = 0; i < distances.length; i++) {
    if (altitudes[i] != null) pts.push([distances[i], altitudes[i]]);
  }
  if (pts.length < 2) {
    ctx.fillStyle = '#A8A8A3';
    ctx.font = '13px sans-serif';
    ctx.fillText('No altitude data in this file', 16, h / 2);
    return;
  }

  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const pad = 12, yPad = (yMax - yMin) * 0.1 || 1;

  const xTo = (x) => pad + (x - xMin) / (xMax - xMin || 1) * (w - 2 * pad);
  const yTo = (y) => h - pad - (y - (yMin - yPad)) / ((yMax + yPad) - (yMin - yPad) || 1) * (h - 2 * pad);

  ctx.beginPath();
  ctx.moveTo(xTo(pts[0][0]), yTo(pts[0][1]));
  for (const [x, y] of pts) ctx.lineTo(xTo(x), yTo(y));
  ctx.strokeStyle = '#c0392b';
  ctx.lineWidth = 2;
  ctx.stroke();
}

boot();
