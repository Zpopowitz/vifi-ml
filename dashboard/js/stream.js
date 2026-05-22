/* Live WebSocket stream to /api/v1/stream.
 *
 * Owns the socket, exponential-backoff reconnect, and the auth-failure
 * bounce (close code 1008). Parsing the message into a vital update is
 * the monitor's job; this module just delivers JSON.
 */

import { CONFIG } from './registry.js';

export function createStream({ getKey, onMessage, onStatus, onAuthFail }) {
  let ws = null;
  let attempt = 0;
  let timer = null;
  let patient = 'default';
  let stopped = false;

  function schedule() {
    if (timer) clearTimeout(timer);
    const delay = Math.min(
      CONFIG.RECONNECT_BASE_MS * 2 ** attempt,
      CONFIG.RECONNECT_MAX_MS,
    );
    attempt += 1;
    timer = setTimeout(open, delay);
  }

  function open() {
    if (ws) {
      try {
        ws.close();
      } catch (e) {
        /* ignore */
      }
      ws = null;
    }
    stopped = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const params = new URLSearchParams({ patient_id: patient });
    const key = getKey();
    if (key) params.set('api_key', key);
    onStatus('connecting', 'Connecting');
    try {
      ws = new WebSocket(`${proto}://${location.host}/api/v1/stream?${params}`);
    } catch (e) {
      onStatus('error', 'Connect failed');
      schedule();
      return;
    }
    ws.onopen = () => {
      attempt = 0;
      onStatus('connecting', 'Subscribing');
    };
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      onMessage(msg);
    };
    ws.onerror = () => onStatus('error', 'Connection error');
    ws.onclose = (ev) => {
      ws = null;
      if (stopped) return;
      // 1008 = policy violation: the WebSocket auth was rejected.
      if (ev && ev.code === 1008) {
        onAuthFail();
        return;
      }
      onStatus('error', 'Disconnected');
      schedule();
    };
  }

  return {
    connect(patientId) {
      if (patientId) patient = patientId;
      attempt = 0;
      open();
    },
    close() {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (ws) {
        try {
          ws.close();
        } catch (e) {
          /* ignore */
        }
        ws = null;
      }
    },
  };
}
