/* Vital registry + runtime config.
 *
 * The registry is the single source of truth for which vitals the
 * monitor shows. Adding apnea, gait, a radar-derived vital, etc. is one
 * entry here: js/monitor.js builds a card, js/stream.js subscribes, and
 * js/chart.js renders it, all driven off this list. No other file
 * hardcodes a vital id.
 */

export const CONFIG = {
  HISTORY_S: 120, // chart window length
  STALE_AFTER_MS: 30_000, // mark data stale after this much silence
  RECONNECT_BASE_MS: 1_000,
  RECONNECT_MAX_MS: 30_000,
  MAE_WINDOW_S: 120, // rolling MAE window
  ROOMS_REFRESH_MS: 10_000,
  HEALTH_REFRESH_MS: 30_000,
  STORAGE_KEY: 'vifiApiKey',
  ROOM_KEY: 'vifiSelectedRoom',
  THEME_KEY: 'vifi-theme',
};

/**
 * One entry per vital sign.
 *   id            stream key in the bus message ("hr" -> hr.predicted.<room>)
 *   label         card title
 *   units         unit shown beside the hero number
 *   referenceUnit unit for the error/MAE figures
 *   referenceLabel ground-truth device name
 *   payloadKey    field carrying the value in the message payload
 *   tolerance     clinical agreement band (bpm); drives the error color
 *   minSpan       minimum chart axis span; a near-flat trace stays calm
 */
export const VITALS = [
  {
    id: 'hr',
    label: 'Heart rate',
    units: 'bpm',
    referenceUnit: 'bpm',
    referenceLabel: 'Polar H10',
    payloadKey: 'hr_bpm',
    tolerance: 5,
    minSpan: 6,
  },
  {
    id: 'rr',
    label: 'Respiratory rate',
    units: 'brpm',
    referenceUnit: 'brpm',
    referenceLabel: 'Vernier GDX-RB',
    payloadKey: 'rr_bpm',
    tolerance: 2,
    minSpan: 3,
  },
];
