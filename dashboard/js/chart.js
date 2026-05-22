/* Live trace renderer.
 *
 * `drawChart` paints one vital's predicted + reference series onto its
 * canvas. Pure: it reads the series and the current theme tokens, and
 * draws. A null predicted value (a gated RR window) breaks the line
 * rather than drawing through the gap.
 */

import { CONFIG } from './registry.js';

function cssVar(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

function drawEmpty(ctx, w, h) {
  ctx.save();
  ctx.strokeStyle = cssVar('--rule');
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(40, h / 2);
  ctx.lineTo(w - 8, h / 2);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = cssVar('--muted');
  ctx.font = '12px "JetBrains Mono", ui-monospace, monospace';
  ctx.textAlign = 'center';
  ctx.fillText('Waiting for data…', w / 2, h / 2 - 8);
  ctx.restore();
}

function drawDot(ctx, x, y, color) {
  ctx.beginPath();
  ctx.arc(x, y, 3, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(x, y, 5.5, 0, Math.PI * 2);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.globalAlpha = 0.35;
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function strokeSeries(ctx, points, xOf, yOf, color, width) {
  if (points.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  // A null value (gated window) breaks the path so the line gaps.
  let drawing = false;
  for (const p of points) {
    if (p.value == null) {
      drawing = false;
      continue;
    }
    const x = xOf(p.ts_ms);
    const y = yOf(p.value);
    if (!drawing) {
      ctx.moveTo(x, y);
      drawing = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}

export function drawChart(canvas, series, vital) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return;
  if (canvas.width !== Math.round(rect.width * dpr)) {
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  const all = [...series.predicted, ...series.reference];
  if (!all.length) {
    drawEmpty(ctx, w, h);
    return;
  }

  const tMax = Math.max(...all.map((p) => p.ts_ms));
  const tMin = tMax - CONFIG.HISTORY_S * 1000;
  const yValues = all.map((p) => p.value).filter((v) => v != null);
  if (!yValues.length) {
    drawEmpty(ctx, w, h);
    return;
  }
  // Fit the axis to the data, with a minimum span so a near-flat trace
  // reads as calm rather than being blown up into full-scale noise.
  const lo = Math.min(...yValues);
  const hi = Math.max(...yValues);
  const mid = (lo + hi) / 2;
  const span = Math.max(hi - lo, vital.minSpan);
  const pad = span * 0.15;
  const yMin = mid - span / 2 - pad;
  const yMax = mid + span / 2 + pad;

  const padL = 38;
  const padR = 10;
  const padT = 10;
  const padB = 20;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;

  const xOf = (t) => padL + ((t - tMin) / (tMax - tMin || 1)) * innerW;
  const yOf = (v) => padT + (1 - (v - yMin) / (yMax - yMin)) * innerH;

  // gridlines + axis ticks
  ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
  ctx.textAlign = 'left';
  ctx.fillStyle = cssVar('--muted');
  ctx.strokeStyle = cssVar('--rule');
  ctx.lineWidth = 1;
  const ticks = 3;
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (yMax - yMin) * (i / ticks);
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillText(Math.round(v).toString(), 4, y + 3);
  }

  strokeSeries(ctx, series.reference, xOf, yOf, cssVar('--data-cool'), 1.75);
  strokeSeries(ctx, series.predicted, xOf, yOf, cssVar('--signal'), 2.5);

  const lastRef = series.reference[series.reference.length - 1];
  if (lastRef && lastRef.value != null) {
    drawDot(ctx, xOf(lastRef.ts_ms), yOf(lastRef.value), cssVar('--data-cool'));
  }
  const lastPred = series.predicted[series.predicted.length - 1];
  if (lastPred && lastPred.value != null) {
    drawDot(ctx, xOf(lastPred.ts_ms), yOf(lastPred.value), cssVar('--signal'));
  }
}
