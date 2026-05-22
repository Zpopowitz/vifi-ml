/* API-key login gate.
 *
 * The key lives in localStorage and is verified against /health. The
 * overlay is shown on cold start with no key, on a failed verify, or
 * after a 401 / WebSocket 1008 elsewhere in the app.
 */

import { CONFIG } from './registry.js';

export function createAuth(els, { onAuthenticated, onSignOut }) {
  function getKey() {
    return localStorage.getItem(CONFIG.STORAGE_KEY) || null;
  }

  function headers() {
    const key = getKey();
    return key ? { Authorization: `Bearer ${key}` } : {};
  }

  function showLogin(errorText) {
    els.overlay.hidden = false;
    els.signout.hidden = true;
    if (errorText) {
      els.error.textContent = errorText;
      els.error.hidden = false;
    } else {
      els.error.textContent = '';
      els.error.hidden = true;
    }
    els.input.focus();
  }

  function hideLogin() {
    els.overlay.hidden = true;
    els.signout.hidden = false;
    els.error.hidden = true;
  }

  async function verify(key) {
    els.submit.disabled = true;
    els.submit.textContent = 'Verifying…';
    try {
      const r = await fetch('/health', {
        headers: key ? { Authorization: `Bearer ${key}` } : {},
      });
      if (r.status === 401) {
        showLogin('Invalid API key. Try again, or ask your system operator.');
        return;
      }
      if (!r.ok) {
        showLogin(`API responded ${r.status}. Try again in a moment.`);
        return;
      }
      localStorage.setItem(CONFIG.STORAGE_KEY, key);
      hideLogin();
      onAuthenticated();
    } catch (e) {
      showLogin('Could not reach the API. Check your connection.');
    } finally {
      els.submit.disabled = false;
      els.submit.textContent = 'Verify and continue';
    }
  }

  els.form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const key = (els.input.value || '').trim();
    if (!key) {
      showLogin('Enter your API key.');
      return;
    }
    verify(key);
  });

  els.show.addEventListener('click', () => {
    const showing = els.input.type === 'text';
    els.input.type = showing ? 'password' : 'text';
    els.show.textContent = showing ? 'show' : 'hide';
  });

  els.signout.addEventListener('click', () => {
    localStorage.removeItem(CONFIG.STORAGE_KEY);
    els.input.value = '';
    if (onSignOut) onSignOut();
    showLogin();
  });

  return {
    getKey,
    headers,
    showLogin,
    start() {
      const key = getKey();
      if (!key) showLogin();
      else verify(key);
    },
  };
}
