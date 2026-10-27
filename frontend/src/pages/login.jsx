import Head from 'next/head';
import { useRouter } from 'next/router';
import { useEffect, useState } from 'react';

import { getAuth, safeNext, setAuth } from '../lib/auth';
import { ROUTES } from '../lib/routes';
import { getErrorMessage, login } from '../services/api';

const field =
  'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-300 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100';

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const destination = () => safeNext(router.query.next, ROUTES.home);

  useEffect(() => {
    let saved = null;
    try { saved = window.localStorage.getItem('evf.theme'); } catch { /* ignore */ }
    const dark = saved ? saved === 'dark' : window.matchMedia?.('(prefers-color-scheme: dark)').matches;
    document.documentElement.classList.toggle('dark', Boolean(dark));
  }, []);

  useEffect(() => {
    if (router.isReady && getAuth()) router.replace(destination());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router.isReady]);

  const submit = async (event) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await login(username.trim(), password);
      setAuth({ token: result.access_token, username: result.username, expiresAt: result.expires_at });
      await router.replace(destination());
    } catch (err) {
      setError(err?.response?.status === 401 ? 'Wrong username or password.' : getErrorMessage(err));
      setBusy(false);
    }
  };

  return (
    <>
      <Head><title>Sign in · EVF</title></Head>
      <main className="flex min-h-screen items-center justify-center bg-slate-100 p-4 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <form onSubmit={submit}
          className="w-full max-w-sm space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-700 dark:bg-slate-900">
          <h1 className="text-lg font-bold tracking-tight">EVF · Sign in</h1>

          <label className="block text-sm">
            <span className="mb-1 block text-slate-600 dark:text-slate-300">Username</span>
            <input className={field} value={username} onChange={(e) => setUsername(e.target.value)}
              autoComplete="username" autoCapitalize="none" spellCheck={false} autoFocus required />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-slate-600 dark:text-slate-300">Password</span>
            <input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password" required />
          </label>

          {error && <p role="alert" className="text-sm text-rose-600 dark:text-rose-400">{error}</p>}

          <button type="submit" disabled={busy || !username.trim() || !password}
            className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </main>
    </>
  );
}
