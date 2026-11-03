/**
 * Who is signed in, for the top bar and the "go to /login" check. The real session is an HttpOnly
 * cookie set by the server: scripts here cannot read it and it is not stored anywhere in the page.
 * Only the username and the expiry time are remembered.
 */
const KEY = 'evf.auth';

export function getAuth() {
  try {
    const value = JSON.parse(window.localStorage.getItem(KEY) || 'null');
    if (value?.username && value.expiresAt * 1000 > Date.now()) return value;
  } catch { /* unreadable: treat as signed out */ }
  return null;
}

export function setAuth({ username, expiresAt }) {
  try { window.localStorage.setItem(KEY, JSON.stringify({ username, expiresAt })); } catch { /* ignore */ }
}

export function clearAuth() {
  try { window.localStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** Only same-site paths may be used as the "go back here after login" target. */
export function safeNext(value, fallback) {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') && !value.startsWith('/login')
    ? value : fallback;
}
