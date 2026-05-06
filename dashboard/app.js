/* ViFi live monitor — vanilla JS WebSocket client.
 *
 * Three responsibilities:
 *   1. Login flow: gate access behind an API key stored in
 *      localStorage. Show overlay on cold start or after a 401.
 *   2. Room discovery: poll /api/v1/rooms; populate the dropdown
 *      with active patient_ids; persist last selection.
 *   3. Live data: connect /api/v1/stream?patient_id=<id> and render
 *      HR + RR predicted vs reference. Reconnect with exponential
 *      backoff. Mark data stale after 30s of silence.
 *
 * No build step, no framework. Drops into any static-file host.
 */

(() => {
    'use strict';

    const HISTORY_S = 120;         // chart window length (seconds)
    const STALE_AFTER_MS = 30_000;
    const RECONNECT_BASE_MS = 1_000;
    const RECONNECT_MAX_MS = 30_000;
    const MAE_WINDOW_S = 120;      // rolling MAE window
    const ROOMS_REFRESH_MS = 10_000;
    const STORAGE_KEY = 'vifiApiKey';
    const ROOM_KEY = 'vifiSelectedRoom';

    // ----- DOM refs -------------------------------------------------------
    const ui = {
        loginOverlay: document.getElementById('login-overlay'),
        loginForm: document.getElementById('login-form'),
        loginInput: document.getElementById('login-key'),
        loginShow: document.getElementById('login-show'),
        loginSubmit: document.getElementById('login-submit'),
        loginError: document.getElementById('login-error'),
        logoutButton: document.getElementById('logout-button'),
        patientSelect: document.getElementById('patient-id'),
        patientRefresh: document.getElementById('patient-refresh'),
        statusEl: document.getElementById('connection-status'),
        statusDot: document.querySelector('#connection-status .status-dot'),
        statusText: document.querySelector('#connection-status .status-text'),
        apiStatus: document.getElementById('api-status'),
        buildVersion: document.getElementById('build-version'),
        cards: {
            hr: document.querySelector('[data-vital="hr"]'),
            rr: document.querySelector('[data-vital="rr"]'),
        },
        canvases: {
            hr: document.getElementById('hr-canvas'),
            rr: document.getElementById('rr-canvas'),
        },
    };

    // ----- State ----------------------------------------------------------
    let ws = null;
    let reconnectAttempt = 0;
    let reconnectTimer = null;
    let staleTimer = null;
    let roomsTimer = null;
    let currentPatient = window.localStorage.getItem(ROOM_KEY) || 'default';

    // Per-vital ring buffer of {ts_ms, predicted?, reference?}.
    const series = {
        hr: { predicted: [], reference: [] },
        rr: { predicted: [], reference: [] },
    };

    // ----- API key / login flow ------------------------------------------
    function getApiKey() {
        return window.localStorage.getItem(STORAGE_KEY) || null;
    }

    function authHeaders() {
        const key = getApiKey();
        return key ? { 'Authorization': `Bearer ${key}` } : {};
    }

    function showLogin(errorText) {
        ui.loginOverlay.hidden = false;
        ui.logoutButton.hidden = true;
        if (errorText) {
            ui.loginError.textContent = errorText;
            ui.loginError.hidden = false;
        } else {
            ui.loginError.textContent = '';
            ui.loginError.hidden = true;
        }
        ui.loginInput.focus();
    }

    function hideLogin() {
        ui.loginOverlay.hidden = true;
        ui.logoutButton.hidden = false;
        ui.loginError.hidden = true;
    }

    async function verifyKeyAndStart(key) {
        // Test the key by hitting /health WITH the key. If the server
        // is configured for AUTH_MODE=api_key the key is checked; if
        // AUTH_MODE=none any key passes (including empty), so we still
        // store it for use as the "this is my session" token.
        ui.loginSubmit.disabled = true;
        ui.loginSubmit.textContent = 'Verifying…';
        try {
            const r = await fetch('/health', {
                headers: key ? { 'Authorization': `Bearer ${key}` } : {},
            });
            if (r.status === 401) {
                showLogin('Invalid API key. Try again, or ask your system operator.');
                return false;
            }
            if (!r.ok) {
                showLogin(`API responded ${r.status}. Try again in a moment.`);
                return false;
            }
            window.localStorage.setItem(STORAGE_KEY, key);
            hideLogin();
            // Boot the rest of the app.
            startApp();
            return true;
        } catch (e) {
            showLogin('Could not reach the API. Check your connection.');
            return false;
        } finally {
            ui.loginSubmit.disabled = false;
            ui.loginSubmit.textContent = 'Verify and continue';
        }
    }

    ui.loginForm.addEventListener('submit', (ev) => {
        ev.preventDefault();
        const key = (ui.loginInput.value || '').trim();
        if (!key) { showLogin('Enter your API key.'); return; }
        verifyKeyAndStart(key);
    });

    ui.loginShow.addEventListener('click', () => {
        const showing = ui.loginInput.type === 'text';
        ui.loginInput.type = showing ? 'password' : 'text';
        ui.loginShow.textContent = showing ? 'show' : 'hide';
    });

    ui.logoutButton.addEventListener('click', () => {
        // Wipe key + reset; force a fresh login.
        window.localStorage.removeItem(STORAGE_KEY);
        if (ws) { try { ws.close(); } catch (_) {} ws = null; }
        if (reconnectTimer) clearTimeout(reconnectTimer);
        if (roomsTimer) clearInterval(roomsTimer);
        ui.loginInput.value = '';
        showLogin();
    });

    // ----- Connection management -----------------------------------------
    function connect() {
        if (ws) {
            try { ws.close(); } catch (e) { /* ignore */ }
            ws = null;
        }

        const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const host = window.location.host;
        const params = new URLSearchParams({ patient_id: currentPatient });
        const apiKey = getApiKey();
        if (apiKey) params.set('api_key', apiKey);

        const url = `${proto}://${host}/api/v1/stream?${params.toString()}`;
        setStatus('idle', 'Connecting');

        try {
            ws = new WebSocket(url);
        } catch (e) {
            setStatus('error', 'Connect failed');
            scheduleReconnect();
            return;
        }

        ws.onopen = () => {
            reconnectAttempt = 0;
            setStatus('idle', 'Subscribing');
        };

        ws.onmessage = (ev) => {
            let msg;
            try { msg = JSON.parse(ev.data); }
            catch (e) {
                console.warn('non-JSON message', ev.data);
                return;
            }
            handleMessage(msg);
        };

        ws.onerror = () => {
            setStatus('error', 'Connection error');
        };

        ws.onclose = (ev) => {
            ws = null;
            // 1008 = policy violation (auth failure on the WebSocket).
            // Bounce to the login overlay so the operator can re-key.
            if (ev && ev.code === 1008) {
                window.localStorage.removeItem(STORAGE_KEY);
                showLogin('Session expired or invalid key. Sign in again.');
                return;
            }
            setStatus('error', 'Disconnected');
            scheduleReconnect();
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) clearTimeout(reconnectTimer);
        const delay = Math.min(
            RECONNECT_BASE_MS * Math.pow(2, reconnectAttempt),
            RECONNECT_MAX_MS,
        );
        reconnectAttempt += 1;
        reconnectTimer = setTimeout(connect, delay);
    }

    // ----- Room discovery ------------------------------------------------
    async function refreshRooms() {
        try {
            ui.patientRefresh.classList.add('spinning');
            const r = await fetch('/api/v1/rooms', { headers: authHeaders() });
            if (r.status === 401) {
                window.localStorage.removeItem(STORAGE_KEY);
                showLogin('Session expired or invalid key. Sign in again.');
                return;
            }
            if (!r.ok) return;
            const body = await r.json();
            updateRoomDropdown(body.rooms || []);
        } catch (e) {
            // Silent — the WebSocket status pill already shows network state.
        } finally {
            // Let the spin animation play out at least one cycle.
            setTimeout(() => ui.patientRefresh.classList.remove('spinning'), 700);
        }
    }

    function updateRoomDropdown(rooms) {
        const sel = ui.patientSelect;
        const previousValue = sel.value || currentPatient;

        // Always include `default` so single-host dev works.
        const knownIds = new Set(rooms.map(r => r.patient_id));
        knownIds.add('default');
        // Remember the currently-selected room even if the bus hasn't
        // seen any of its messages recently.
        knownIds.add(previousValue);

        // Build a sorted list — recent rooms first, then alpha.
        const ordered = [];
        for (const r of rooms) {
            ordered.push({
                id: r.patient_id,
                last: r.last_seen_ms,
                topics: r.topics_with_data || [],
            });
        }
        // Add any known IDs that didn't come back from the bus.
        for (const id of knownIds) {
            if (!ordered.some(o => o.id === id)) {
                ordered.push({ id, last: 0, topics: [] });
            }
        }

        // Re-render only if the set or ordering changed (avoids
        // dropdown flicker mid-selection).
        const newSig = ordered.map(o => o.id).join('|');
        if (sel.dataset.sig === newSig) return;

        sel.innerHTML = '';
        for (const o of ordered) {
            const opt = document.createElement('option');
            opt.value = o.id;
            const tagsTxt = o.topics.length
                ? ` (${o.topics.length} stream${o.topics.length > 1 ? 's' : ''})`
                : '';
            opt.textContent = o.id + tagsTxt;
            sel.appendChild(opt);
        }
        sel.dataset.sig = newSig;
        sel.value = previousValue;
        if (sel.value !== previousValue) {
            // The previous selection is no longer in the list; fall back.
            sel.value = ordered[0]?.id || 'default';
        }
    }

    ui.patientSelect.addEventListener('change', () => {
        const next = (ui.patientSelect.value || 'default').trim();
        if (next === currentPatient) return;
        currentPatient = next;
        window.localStorage.setItem(ROOM_KEY, next);
        clearSeries();
        renderAllCards();
        connect();
    });

    ui.patientRefresh.addEventListener('click', () => refreshRooms());

    function clearSeries() {
        for (const v of ['hr', 'rr']) {
            series[v].predicted = [];
            series[v].reference = [];
        }
    }

    // ----- Status pill ----------------------------------------------------
    function setStatus(kind, text) {
        const cls = `status status-${kind}`;
        ui.statusEl.className = cls;
        ui.statusText.textContent = text;
    }

    // ----- Message dispatcher --------------------------------------------
    function handleMessage(msg) {
        if (msg.type === 'hello') {
            setStatus('live', 'Live');
            ui.apiStatus.textContent = `API: ok`;
            ui.buildVersion.textContent = `model ${msg.model_version}`;
            return;
        }

        const stream = msg.stream;
        const role = msg.role;
        if (!stream || !role) return;
        if (!['hr', 'rr'].includes(stream)) return;
        if (!['predicted', 'reference'].includes(role)) return;
        if (!series[stream][role]) return;

        const payload = msg.payload || {};
        const valueKey = stream === 'hr' ? 'hr_bpm' : 'rr_bpm';
        const value = payload[valueKey];
        if (typeof value !== 'number') return;

        const ts_ms = msg.ts_ms || Date.now();
        series[stream][role].push({ ts_ms, value, payload });
        trim(series[stream][role]);
        renderCard(stream);
        resetStaleTimer();
    }

    function trim(buf) {
        if (!buf.length) return;
        const cutoff = buf[buf.length - 1].ts_ms - HISTORY_S * 1000;
        let i = 0;
        while (i < buf.length && buf[i].ts_ms < cutoff) i++;
        if (i > 0) buf.splice(0, i);
    }

    // ----- Stale detector -------------------------------------------------
    function resetStaleTimer() {
        if (staleTimer) clearTimeout(staleTimer);
        staleTimer = setTimeout(() => {
            if (!ws || ws.readyState !== WebSocket.OPEN) return;
            setStatus('stale', 'No new data');
            for (const v of ['hr', 'rr']) {
                ui.cards[v].querySelectorAll('.vital-value').forEach(el => {
                    el.classList.add('stale');
                });
            }
        }, STALE_AFTER_MS);
    }

    // ----- Render: vital card --------------------------------------------
    const fmt = {
        bpm: (v) => v == null ? '—' : Math.round(v),
        bpm1: (v) => v == null ? '—' : v.toFixed(1),
        confidence: (c) => c == null ? '—' : `confidence ${(c * 100).toFixed(0)}%`,
        signed: (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(1)}`,
    };

    function lastValue(arr) {
        return arr.length ? arr[arr.length - 1] : null;
    }

    function renderCard(streamKind) {
        const card = ui.cards[streamKind];
        const buf = series[streamKind];
        const pred = lastValue(buf.predicted);
        const ref = lastValue(buf.reference);

        const set = (field, text) => {
            const el = card.querySelector(`[data-field="${field}"]`);
            if (el) {
                el.textContent = text;
                el.classList.remove('stale');
            }
        };

        set('predicted', fmt.bpm(pred ? pred.value : null));
        set('reference', fmt.bpm(ref ? ref.value : null));
        set('confidence',
            fmt.confidence(pred && pred.payload[`${streamKind}_confidence`]));

        const err = (pred && ref) ? pred.value - ref.value : null;
        const errorEl = card.querySelector('[data-field="error"]');
        if (errorEl) {
            errorEl.textContent = err == null ? '—' : fmt.signed(err);
            errorEl.classList.remove('error-good', 'error-warn', 'error-bad');
            if (err != null) {
                const abs = Math.abs(err);
                const tol = streamKind === 'hr' ? 5 : 2;
                if (abs <= tol)         errorEl.classList.add('error-good');
                else if (abs <= tol * 2) errorEl.classList.add('error-warn');
                else                     errorEl.classList.add('error-bad');
            }
        }

        const mae = computeMae(buf, MAE_WINDOW_S);
        const maeEl = card.querySelector('[data-field="mae"]');
        if (maeEl) {
            const units = streamKind === 'hr' ? 'bpm' : 'brpm';
            maeEl.textContent = mae.n
                ? `MAE 2 min: ${mae.value.toFixed(2)} ${units} (n=${mae.n})`
                : `MAE 2 min: —`;
        }

        drawChart(streamKind);
    }

    function renderAllCards() {
        renderCard('hr');
        renderCard('rr');
    }

    function computeMae(buf, windowS) {
        if (!buf.predicted.length || !buf.reference.length) {
            return { value: 0, n: 0 };
        }
        const cutoff = Date.now() - windowS * 1000;
        const refs = buf.reference.filter(p => p.ts_ms >= cutoff);
        const preds = buf.predicted.filter(p => p.ts_ms >= cutoff);
        if (!refs.length || !preds.length) return { value: 0, n: 0 };
        let sum = 0, n = 0;
        for (const p of preds) {
            const r = nearest(refs, p.ts_ms);
            if (r && Math.abs(r.ts_ms - p.ts_ms) < 5_000) {
                sum += Math.abs(p.value - r.value);
                n += 1;
            }
        }
        return { value: n ? sum / n : 0, n };
    }

    function nearest(sortedByTs, ts) {
        if (!sortedByTs.length) return null;
        let best = sortedByTs[0];
        let bestD = Math.abs(best.ts_ms - ts);
        for (let i = 1; i < sortedByTs.length; i++) {
            const d = Math.abs(sortedByTs[i].ts_ms - ts);
            if (d < bestD) { bestD = d; best = sortedByTs[i]; }
        }
        return best;
    }

    // ----- Render: chart --------------------------------------------------
    function drawChart(streamKind) {
        const canvas = ui.canvases[streamKind];
        const ctx = canvas.getContext('2d');

        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        if (canvas.width !== rect.width * dpr) {
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            ctx.scale(dpr, dpr);
        }
        const W = rect.width;
        const H = rect.height;

        ctx.clearRect(0, 0, W, H);
        const buf = series[streamKind];

        if (!buf.predicted.length && !buf.reference.length) {
            drawEmpty(ctx, W, H);
            return;
        }

        const all = [...buf.predicted, ...buf.reference];
        if (!all.length) { drawEmpty(ctx, W, H); return; }

        const tMax = Math.max(...all.map(p => p.ts_ms));
        const tMin = tMax - HISTORY_S * 1000;
        const yValues = all.map(p => p.value);

        const yMinFloor = streamKind === 'hr' ? 50 : 8;
        const yMaxFloor = streamKind === 'hr' ? 110 : 30;
        let yMin = Math.min(yMinFloor, ...yValues);
        let yMax = Math.max(yMaxFloor, ...yValues);
        const span = Math.max(yMax - yMin, 5);
        yMin = yMin - span * 0.1;
        yMax = yMax + span * 0.1;

        const padL = 36, padR = 8, padT = 8, padB = 18;
        const innerW = W - padL - padR;
        const innerH = H - padT - padB;

        const xOf = (t) => padL + ((t - tMin) / (tMax - tMin || 1)) * innerW;
        const yOf = (v) => padT + (1 - (v - yMin) / (yMax - yMin)) * innerH;

        ctx.font = '11px ui-monospace, SFMono-Regular, monospace';
        ctx.fillStyle = getCSS('--ink-faint');
        ctx.strokeStyle = getCSS('--border');
        ctx.lineWidth = 1;
        const ticks = 3;
        for (let i = 0; i <= ticks; i++) {
            const v = yMin + (yMax - yMin) * (i / ticks);
            const y = yOf(v);
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(W - padR, y);
            ctx.stroke();
            ctx.fillText(Math.round(v).toString(), 4, y + 3);
        }

        if (buf.reference.length > 1) {
            ctx.strokeStyle = getCSS('--reference');
            ctx.lineWidth = 1.75;
            ctx.beginPath();
            buf.reference.forEach((p, i) => {
                const x = xOf(p.ts_ms);
                const y = yOf(p.value);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
        }

        if (buf.predicted.length > 1) {
            ctx.strokeStyle = getCSS('--accent');
            ctx.lineWidth = 2.25;
            ctx.beginPath();
            buf.predicted.forEach((p, i) => {
                const x = xOf(p.ts_ms);
                const y = yOf(p.value);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
        }

        if (buf.reference.length) {
            const last = buf.reference[buf.reference.length - 1];
            drawDot(ctx, xOf(last.ts_ms), yOf(last.value),
                    getCSS('--reference'));
        }
        if (buf.predicted.length) {
            const last = buf.predicted[buf.predicted.length - 1];
            drawDot(ctx, xOf(last.ts_ms), yOf(last.value),
                    getCSS('--accent'));
        }
    }

    function drawDot(ctx, x, y, color) {
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, 5, 0, Math.PI * 2);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = 0.4;
        ctx.stroke();
        ctx.globalAlpha = 1.0;
    }

    function drawEmpty(ctx, W, H) {
        ctx.save();
        ctx.strokeStyle = getCSS('--border');
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(40, H / 2);
        ctx.lineTo(W - 8, H / 2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = getCSS('--ink-faint');
        ctx.font = '12px ui-monospace, monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for data…', W / 2, H / 2 - 8);
        ctx.restore();
    }

    function getCSS(varName) {
        return getComputedStyle(document.documentElement)
            .getPropertyValue(varName).trim();
    }

    window.addEventListener('resize', () => {
        renderCard('hr');
        renderCard('rr');
    });

    // ----- Boot ----------------------------------------------------------
    function startApp() {
        // Start everything that needs an authenticated session.
        ui.patientSelect.value = currentPatient;
        connect();
        renderAllCards();
        refreshRooms();
        if (roomsTimer) clearInterval(roomsTimer);
        roomsTimer = setInterval(refreshRooms, ROOMS_REFRESH_MS);
        pollHealth();
        if (window._healthInterval) clearInterval(window._healthInterval);
        window._healthInterval = setInterval(pollHealth, 30_000);
    }

    async function pollHealth() {
        try {
            const r = await fetch('/health', { headers: authHeaders() });
            const ok = r.ok;
            ui.apiStatus.textContent = ok ? 'API: ok' : `API: ${r.status}`;
            if (ok) {
                const body = await r.json().catch(() => ({}));
                if (body.model_version) {
                    ui.buildVersion.textContent = `model ${body.model_version}`;
                }
            }
            if (r.status === 401) {
                window.localStorage.removeItem(STORAGE_KEY);
                showLogin('Session expired or invalid key. Sign in again.');
            }
        } catch (e) {
            ui.apiStatus.textContent = 'API: unreachable';
        }
    }

    // Cold start: do we already have a key?
    const existingKey = getApiKey();
    if (!existingKey) {
        // No key yet — show the login overlay. Don't auto-attempt.
        showLogin();
    } else {
        // Verify cached key. If still valid, hide overlay + start.
        verifyKeyAndStart(existingKey).then(ok => {
            if (!ok) {
                // verifyKeyAndStart already showed the appropriate error.
            }
        });
    }
})();
