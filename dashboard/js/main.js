/* App orchestrator.
 *
 * Wires the four pieces: the API-key gate (auth), the vital-card view
 * (monitor), the live WebSocket (stream), plus room discovery, health
 * polling, and the theme toggle. This is the only module that touches
 * the topbar / footer chrome.
 */

import { CONFIG } from './registry.js';
import { createAuth } from './auth.js';
import { createMonitor } from './monitor.js';
import { createStream } from './stream.js';

const $ = (id) => document.getElementById(id);

const els = {
  monitor: $('monitor'),
  template: $('vital-card'),
  roomSelect: $('room-select'),
  roomRefresh: $('room-refresh'),
  status: $('conn-status'),
  statusText: document.querySelector('#conn-status .status__text'),
  themeToggle: $('theme-toggle'),
  apiStatus: $('api-status'),
  buildVersion: $('build-version'),
};

let currentPatient = localStorage.getItem(CONFIG.ROOM_KEY) || 'default';
let roomsTimer = null;
let healthTimer = null;

const monitor = createMonitor(els.monitor, els.template);

function setStatus(state, text) {
  els.status.dataset.state = state;
  els.statusText.textContent = text;
}

function bounceToLogin() {
  localStorage.removeItem(CONFIG.STORAGE_KEY);
  teardown();
  auth.showLogin('Session expired or invalid key. Sign in again.');
}

function teardown() {
  stream.close();
  if (roomsTimer) clearInterval(roomsTimer);
  if (healthTimer) clearInterval(healthTimer);
}

function handleMessage(msg) {
  if (msg.type === 'hello') {
    setStatus('live', 'Live');
    els.apiStatus.textContent = 'API: ok';
    if (msg.model_version) {
      els.buildVersion.textContent = `model ${msg.model_version}`;
    }
    return;
  }
  if (msg.stream && msg.role) monitor.applyMessage(msg);
}

function startApp() {
  els.roomSelect.value = currentPatient;
  stream.connect(currentPatient);
  monitor.renderAll();
  refreshRooms();
  pollHealth();
  if (roomsTimer) clearInterval(roomsTimer);
  if (healthTimer) clearInterval(healthTimer);
  roomsTimer = setInterval(refreshRooms, CONFIG.ROOMS_REFRESH_MS);
  healthTimer = setInterval(pollHealth, CONFIG.HEALTH_REFRESH_MS);
}

const auth = createAuth(
  {
    overlay: $('login-overlay'),
    form: $('login-form'),
    input: $('login-key'),
    show: $('login-show'),
    submit: $('login-submit'),
    error: $('login-error'),
    signout: $('signout'),
  },
  { onAuthenticated: startApp, onSignOut: teardown },
);

const stream = createStream({
  getKey: () => auth.getKey(),
  onStatus: setStatus,
  onMessage: handleMessage,
  onAuthFail: bounceToLogin,
});

// ---- Room discovery -----------------------------------------------------
async function refreshRooms() {
  try {
    const r = await fetch('/api/v1/rooms', { headers: auth.headers() });
    if (r.status === 401) {
      bounceToLogin();
      return;
    }
    if (!r.ok) return;
    const body = await r.json();
    updateRoomDropdown(body.rooms || []);
  } catch (e) {
    /* the status pill already reflects network state */
  }
}

function updateRoomDropdown(rooms) {
  const sel = els.roomSelect;
  const previous = sel.value || currentPatient;
  const ordered = rooms.map((r) => ({
    id: r.patient_id,
    topics: r.topics_with_data || [],
  }));
  // Always offer `default` and whatever is currently selected.
  for (const id of ['default', previous]) {
    if (!ordered.some((o) => o.id === id)) ordered.push({ id, topics: [] });
  }
  const sig = ordered.map((o) => o.id).join('|');
  if (sel.dataset.sig === sig) return; // unchanged — don't flicker mid-selection
  sel.innerHTML = '';
  for (const o of ordered) {
    const opt = document.createElement('option');
    opt.value = o.id;
    const n = o.topics.length;
    opt.textContent = n ? `${o.id} (${n} stream${n > 1 ? 's' : ''})` : o.id;
    sel.appendChild(opt);
  }
  sel.dataset.sig = sig;
  sel.value = ordered.some((o) => o.id === previous) ? previous : ordered[0].id;
}

els.roomSelect.addEventListener('change', () => {
  const next = (els.roomSelect.value || 'default').trim();
  if (next === currentPatient) return;
  currentPatient = next;
  localStorage.setItem(CONFIG.ROOM_KEY, next);
  monitor.clear();
  stream.connect(next);
});

els.roomRefresh.addEventListener('click', refreshRooms);

// ---- Health -------------------------------------------------------------
async function pollHealth() {
  try {
    const r = await fetch('/health', { headers: auth.headers() });
    els.apiStatus.textContent = r.ok ? 'API: ok' : `API: ${r.status}`;
    if (r.ok) {
      const body = await r.json().catch(() => ({}));
      if (body.model_version) {
        els.buildVersion.textContent = `model ${body.model_version}`;
      }
    }
    if (r.status === 401) bounceToLogin();
  } catch (e) {
    els.apiStatus.textContent = 'API: unreachable';
  }
}

// ---- Theme --------------------------------------------------------------
els.themeToggle.addEventListener('click', () => {
  const next =
    document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(CONFIG.THEME_KEY, next);
  } catch (e) {
    /* ignore */
  }
  monitor.renderAll(); // repaint canvases with the new token colors
});

// ---- Resize -------------------------------------------------------------
let resizeRaf = 0;
window.addEventListener('resize', () => {
  cancelAnimationFrame(resizeRaf);
  resizeRaf = requestAnimationFrame(() => monitor.renderAll());
});

// ---- Boot ---------------------------------------------------------------
monitor.renderAll(); // show the empty-state cards immediately
auth.start();
