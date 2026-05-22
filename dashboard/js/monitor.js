/* The vital-card view.
 *
 * `createMonitor` builds one card per registered vital (cloning the
 * #vital-card template), owns the rolling predicted/reference series,
 * and renders. It is registry-driven: it never names a vital directly.
 */

import { CONFIG, VITALS } from './registry.js';
import { drawChart } from './chart.js';

const fmt = {
  num: (v) => (v == null ? '—' : String(Math.round(v))),
  signed: (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(1)}`),
  confidence: (c) => (c == null ? '' : `Confidence ${Math.round(c * 100)}%`),
};

function trim(buf) {
  if (!buf.length) return;
  const cutoff = buf[buf.length - 1].ts_ms - CONFIG.HISTORY_S * 1000;
  let i = 0;
  while (i < buf.length && buf[i].ts_ms < cutoff) i += 1;
  if (i > 0) buf.splice(0, i);
}

function nearest(points, ts) {
  let best = null;
  let bestD = Infinity;
  for (const p of points) {
    const d = Math.abs(p.ts_ms - ts);
    if (d < bestD) {
      bestD = d;
      best = p;
    }
  }
  return best;
}

function computeMae(series) {
  const cutoff = Date.now() - CONFIG.MAE_WINDOW_S * 1000;
  const preds = series.predicted.filter((p) => p.ts_ms >= cutoff);
  const refs = series.reference.filter((p) => p.ts_ms >= cutoff);
  if (!preds.length || !refs.length) return { value: 0, n: 0 };
  let sum = 0;
  let n = 0;
  for (const p of preds) {
    if (p.value == null) continue; // gated / unavailable reading
    const r = nearest(refs, p.ts_ms);
    if (r && r.value != null && Math.abs(r.ts_ms - p.ts_ms) < 5000) {
      sum += Math.abs(p.value - r.value);
      n += 1;
    }
  }
  return { value: n ? sum / n : 0, n };
}

export function createMonitor(monitorEl, template) {
  const cards = new Map();
  let staleTimer = null;

  for (const vital of VITALS) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.vital = vital.id;
    const fields = {};
    node.querySelectorAll('[data-field]').forEach((el) => {
      fields[el.dataset.field] = el;
    });
    fields.title.textContent = vital.label;
    fields.units.textContent = vital.units;
    fields.source.textContent = vital.referenceLabel;
    monitorEl.appendChild(node);
    cards.set(vital.id, { vital, fields, series: { predicted: [], reference: [] } });
  }

  function setBadge(card, label, state) {
    card.fields.status.textContent = label;
    if (state) card.fields.status.dataset.state = state;
    else card.fields.status.removeAttribute('data-state');
  }

  function renderCard(card) {
    const { vital, fields, series } = card;
    const pred = series.predicted[series.predicted.length - 1] || null;
    const ref = series.reference[series.reference.length - 1] || null;
    const predValue = pred ? pred.value : null;

    fields.predicted.textContent = fmt.num(predValue);
    fields.predicted.classList.toggle('is-empty', predValue == null);

    if (!pred) setBadge(card, 'Waiting', null);
    else if (predValue == null) setBadge(card, 'Gated', 'gated');
    else setBadge(card, 'Live', 'live');

    fields.confidence.textContent = fmt.confidence(
      pred ? pred.payload[`${vital.id}_confidence`] : null,
    );
    fields.reference.textContent = fmt.num(ref ? ref.value : null);

    const err =
      pred && ref && pred.value != null && ref.value != null
        ? pred.value - ref.value
        : null;
    fields.error.textContent = fmt.signed(err);
    fields.error.classList.remove('error-good', 'error-warn', 'error-bad');
    if (err != null) {
      const abs = Math.abs(err);
      if (abs <= vital.tolerance) fields.error.classList.add('error-good');
      else if (abs <= vital.tolerance * 2) fields.error.classList.add('error-warn');
      else fields.error.classList.add('error-bad');
    }

    const mae = computeMae(series);
    fields.mae.textContent = mae.n
      ? `MAE 2 min: ${mae.value.toFixed(2)} ${vital.referenceUnit} (n=${mae.n})`
      : 'MAE 2 min: —';

    drawChart(fields.canvas, series, vital);
  }

  function renderAll() {
    for (const card of cards.values()) renderCard(card);
  }

  function resetStale() {
    if (staleTimer) clearTimeout(staleTimer);
    staleTimer = setTimeout(() => {
      for (const card of cards.values()) {
        if (card.series.predicted.length || card.series.reference.length) {
          setBadge(card, 'Stale', 'stale');
        }
      }
    }, CONFIG.STALE_AFTER_MS);
  }

  function applyMessage(msg) {
    const card = cards.get(msg.stream);
    if (!card) return false;
    if (msg.role !== 'predicted' && msg.role !== 'reference') return false;
    const payload = msg.payload || {};
    const raw = payload[card.vital.payloadKey];
    // Null is a gated reading and is kept (the card blanks, the chart
    // gaps). A non-numeric value is malformed and dropped.
    if (raw != null && typeof raw !== 'number') return false;
    card.series[msg.role].push({
      ts_ms: msg.ts_ms || Date.now(),
      value: typeof raw === 'number' ? raw : null,
      payload,
    });
    trim(card.series[msg.role]);
    renderCard(card);
    resetStale();
    return true;
  }

  function clear() {
    for (const card of cards.values()) {
      card.series.predicted = [];
      card.series.reference = [];
    }
    renderAll();
  }

  return { applyMessage, renderAll, clear };
}
