import Head from 'next/head';
import Link from 'next/link';

import NavBar from '../components/NavBar';
import { ROUTES } from '../lib/routes';
import RequireAuth from '../components/RequireAuth';

const ITEMS = [
  { key: 'sites', title: 'Sites', text: 'Browse every site and its events, look at the evidence, download a site as .xlsx or delete it.' },
  { key: 'analytics', title: 'Analytics', text: 'Precision, image quality and make / model / color accuracy for each site.' },
  { key: 'import', title: 'Import data', text: 'Add images and data files to a new or existing site.' },
  { key: 'review', title: 'Review', text: 'Go through events that are waiting for a verdict.' },
];

function HomePageInner() {
  return (
    <>
      <Head>
        <title>Home · EVF</title>
      </Head>
      <div className="min-h-screen bg-slate-100 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <NavBar current="home" />
        <main className="mx-auto max-w-3xl px-4 py-10">
          <h1 className="text-2xl font-semibold tracking-tight">Automated Event Validation</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Choose where to go.</p>

          <ul className="mt-6 divide-y divide-slate-200 overflow-hidden rounded-lg border border-slate-200 bg-white dark:divide-slate-700 dark:border-slate-700 dark:bg-slate-900">
            {ITEMS.map((item) => (
              <li key={item.key}>
                <Link href={ROUTES[item.key]} className="block px-4 py-4 hover:bg-slate-50 dark:hover:bg-slate-800/60">
                  <span className="block font-medium">{item.title}</span>
                  <span className="block text-sm text-slate-600 dark:text-slate-400">{item.text}</span>
                </Link>
              </li>
            ))}
          </ul>
        </main>
      </div>
    </>
  );
}

export default function HomePage() {
  return (
    <RequireAuth>
      <HomePageInner />
    </RequireAuth>
  );
}
