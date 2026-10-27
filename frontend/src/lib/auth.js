/** The signed-in user, kept in this browser (localStorage). Nothing here is a secret from the user. */
const KEY = 'evf.auth';

export function getAuth() {
  try {
    const value = JSON.parse(window.localStorage.getItem(KEY) || 'null');
    if (value?.token && value.expiresAt * 1000 > Date.now()) return value;
  } catch { /* unreadable: treat as signed out */ }
  return null;
}

export function setAuth({ token, username, expiresAt }) {
  try { window.localStorage.setItem(KEY, JSON.stringify({ token, username, expiresAt })); } catch { /* ignore */ }
}

export function clearAuth() {
  try { window.localStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** Only same-site paths may be used as the "go back here after login" target. */
export function safeNext(value, fallback) {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') && !value.startsWith('/login')
    ? value : fallback;
}
