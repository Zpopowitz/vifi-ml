/* ViFi live monitor — vanilla JS WebSocket client.
 *
 * Subscribes to /api/v1/stream?patient_id=<id> and renders live HR + RR
 * predicted vs reference. No build step, no framework — drops into any
 * static-file host. Connects on load, reconnects on disconnect with
 * exponential backoff, marks data stale after 30s of silence.
 */

(() => {
    'use strict';

    const API_KEY = ''; // set via window.localStorage.vifiApiKey if auth on
    const HISTORY_S = 120;     // chart window length (seconds)
    const STALE_AFTER_MS = 30_000;
    const RECONNECT_BASE_MS = 1_000;
    const RECONNECT_MAX_MS = 30_000;
    const MAE_WINDOW_S = 120;  // rolling MAE window

    // ----- DOM refs -------------------------------------------------------
    const ui = {
        patientInput: document.getElementById('patient-id'),
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
    let currentPatient = ui.patientInput.value || 'default';

    // Per-vital ring buffer of {ts_ms, predicted?, reference?}.
    // Sampling rates differ (predicted ~0.5 Hz, reference ~1 Hz), so we
    // store events as they arrive and the chart reduces them on render.
    const series = {
        hr: { predicted: [], reference: [] },
        rr: { predicted: [], reference: [] },
    };

    // ----- API key (optional, for auth-on deployments) --------------------
    function getApiKey() {
        const stored = window.localStorage.getItem('vifiApiKey');
        return API_KEY || stored || null;
    }

    // ----- Connection management ------------------------------------------
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
            // Browser doesn't expose detail; the close event follows.
            setStatus('error', 'Connection error');
        };

        ws.onclose = () => {
            ws = null;
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

    // ----- Patient switching ---------------------------------------------
    ui.patientInput.addEventListener('change', () => {
        const next = (ui.patientInput.value || 'default').trim();
        if (next === currentPatient) return;
        currentPatient = next;
        // Clear all series so the new patient starts with a blank slate.
        clearSeries();
        renderAllCards();
        connect();
    });

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

        // Topic envelope: {type: "hr.predicted", stream, role, payload, ts_ms, ...}
        const stream = msg.stream;     // "hr" or "rr"
        const role = msg.role;         // "predicted" or "reference"
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
        // Trim from the head until in-window.
        let i = 0;
        while (i < buf.length && buf[i].ts_ms < cutoff) i++;
        if (i > 0) buf.splice(0, i);
    }

    // ----- Stale detector -------------------------------------------------
    function resetStaleTimer() {
        if (staleTimer) clearTimeout(staleTimer);
        // Only mark stale if we're connected; otherwise the status pill
        // already says "Disconnected".
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

        // Error: difference at the most recent matched point.
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

        // MAE over the rolling window.
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
        // Pair each predicted point to the nearest reference point in time.
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
    // Custom Canvas line chart. Two reasons to roll our own instead of
    // pulling Chart.js:
    //   (a) Two stacked numeric streams + simple time-axis is trivial to
    //       draw; a 200KB lib for this is overkill.
    //   (b) Zero JS deps means the dashboard works in network-isolated
    //       clinic environments — no CDN dependency.

    function drawChart(streamKind) {
        const canvas = ui.canvases[streamKind];
        const ctx = canvas.getContext('2d');

        // Handle device-pixel-ratio for sharp rendering.
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

        // No data — draw a dotted baseline.
        if (!buf.predicted.length && !buf.reference.length) {
            drawEmpty(ctx, W, H);
            return;
        }

        const all = [...buf.predicted, ...buf.reference];
        if (!all.length) {
            drawEmpty(ctx, W, H);
            return;
        }

        const tMax = Math.max(...all.map(p => p.ts_ms));
        const tMin = tMax - HISTORY_S * 1000;
        const yValues = all.map(p => p.value);

        // Fixed sensible y-bands per vital, padded to data so the line
        // never clips. Floors keep the chart steady when only one
        // value has arrived.
        const yMinFloor = streamKind === 'hr' ? 50 : 8;
        const yMaxFloor = streamKind === 'hr' ? 110 : 30;
        let yMin = Math.min(yMinFloor, ...yValues);
        let yMax = Math.max(yMaxFloor, ...yValues);
        // Pad 10%.
        const span = Math.max(yMax - yMin, 5);
        yMin = yMin - span * 0.1;
        yMax = yMax + span * 0.1;

        const padL = 36, padR = 8, padT = 8, padB = 18;
        const innerW = W - padL - padR;
        const innerH = H - padT - padB;

        const xOf = (t) => padL + ((t - tMin) / (tMax - tMin || 1)) * innerW;
        const yOf = (v) => padT + (1 - (v - yMin) / (yMax - yMin)) * innerH;

        // Y-axis labels (3 ticks) + horizontal gridlines
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

        // Reference line (drawn first so predicted overlays).
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

        // Predicted line
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

        // Latest dots
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

    // ----- Periodic redraw (handles new device-pixel-ratio on monitor swap) ----
    window.addEventListener('resize', () => {
        renderCard('hr');
        renderCard('rr');
    });

    // ----- Boot ----------------------------------------------------------
    connect();
    renderAllCards();

    // Re-poll /health to surface API status in the footer (cosmetic).
    async function pollHealth() {
        try {
            const apiKey = getApiKey();
            const headers = apiKey ? { 'Authorization': `Bearer ${apiKey}` } : {};
            const r = await fetch('/health', { headers });
            const ok = r.ok;
            ui.apiStatus.textContent = ok ? 'API: ok' : `API: ${r.status}`;
            if (ok) {
                const body = await r.json().catch(() => ({}));
                if (body.model_version) {
                    ui.buildVersion.textContent = `model ${body.model_version}`;
                }
            }
        } catch (e) {
            ui.apiStatus.textContent = 'API: unreachable';
        }
    }

    pollHealth();
    setInterval(pollHealth, 30_000);
})();
