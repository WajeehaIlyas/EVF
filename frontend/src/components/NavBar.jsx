import Link from 'next/link';
import { useRouter } from 'next/router';
import { useEffect, useState } from 'react';

import { clearAuth, getAuth } from '../lib/auth';
import { NAV_ITEMS, ROUTES } from '../lib/routes';

const THEME_KEY = 'evf.theme';

/**
 * The top bar shown on every page: links to all pages (the current one highlighted), always on
 * screen, plus the light / dark switch.
 *
 *   <NavBar current="sites" />      current: home | sites | analytics | import | review
 *
 * The bar owns the theme: it reads the saved choice, puts the `dark` class on <html> and saves
 * it, so a page does not need any theme code of its own.
 */
export default function NavBar({ current }) {
  const router = useRouter();
  const [theme, setTheme] = useState(null);
  const [username, setUsername] = useState('');

  useEffect(() => { setUsername(getAuth()?.username || ''); }, []);

  const signOut = () => {
    clearAuth();
    router.replace('/login');
  };

  useEffect(() => {
    let saved = null;
    try { saved = window.localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
    const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;
    setTheme(saved || (prefersDark ? 'dark' : 'light'));
  }, []);

  useEffect(() => {
    if (!theme) return;
    document.documentElement.classList.toggle('dark', theme === 'dark');
    try { window.localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  }, [theme]);

  return (
    <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
      <nav aria-label="Main" className="mx-auto flex max-w-[1500px] items-center gap-1 px-3 py-2">
        <span className="mr-3 text-base font-bold tracking-tight">EVF</span>
        <ul className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {NAV_ITEMS.map(({ key, label }) => {
            const active = key === current;
            return (
              <li key={key}>
                <Link
                  href={ROUTES[key]}
                  aria-current={active ? 'page' : undefined}
                  className={`block whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium ${
                    active
                      ? 'bg-indigo-600 text-white'
                      : 'text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'}`}
                >
                  {label}
                </Link>
              </li>
            );
          })}
        </ul>
        {username && (
          <span className="hidden whitespace-nowrap px-2 text-sm text-slate-600 sm:inline dark:text-slate-300">{username}</span>
        )}
        <button
          type="button"
          onClick={signOut}
          className="rounded-lg border border-slate-300 px-2.5 py-1 text-sm text-slate-700 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
        >
          Log out
        </button>
        <button
          type="button"
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          aria-label="Toggle dark mode"
          className="rounded-lg border border-slate-300 px-2.5 py-1 text-sm dark:border-slate-600"
        >
          {theme === 'dark' ? '☀' : '☾'}
        </button>
      </nav>
    </header>
  );
}
