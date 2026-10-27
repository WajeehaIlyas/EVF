/**
 * Where each page lives. The top bar and every "go to" link read from here, so a page that moves
 * only needs changing in this one place.
 *
 * Home is at /home and Review stays at "/" until you decide to swap them:
 *   1. rename pages/index.jsx (the Review page) to pages/review.jsx
 *   2. rename pages/home.jsx to pages/index.jsx
 *   3. set  home: '/'  and  review: '/review'  below
 */
export const ROUTES = {
  home: '/home',
  sites: '/sites',
  analytics: '/analytics',
  import: '/import',
  review: '/',
};

/** Top bar order. */
export const NAV_ITEMS = [
  { key: 'home', label: 'Home' },
  { key: 'sites', label: 'Sites' },
  { key: 'analytics', label: 'Analytics' },
  { key: 'import', label: 'Import data' },
  { key: 'review', label: 'Review' },
];
