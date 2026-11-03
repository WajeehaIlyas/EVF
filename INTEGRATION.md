# Update 5 (cumulative: includes updates 3 and 4)

Copy these files over your project (same paths), then restart the backend and the frontend.

## Backend
* Only `analytics.router` needs registering, and you already have it. The zip export, the Sites page
  endpoints and DELETE site are now mounted under it:
  `GET /analytics/browse/events`, `DELETE /analytics/sites/{id}`, `/analytics/classified-exports/...`.
  (Earlier they were separate routers, which is why the Sites page and Delete answered "Not Found".)
  You can remove `site_browser` / `classified_exports` from your router list if you added them.
* `pip install -e backend` once (openpyxl). `init_db()` adds new columns on restart.

## Frontend
* New: `lib/routes.js`, `components/NavBar.jsx`, `pages/home.jsx`, `pages/import.jsx`
  (serves ImportPage at /import), `components/DeleteSiteDialog.jsx`.
* Changed: `pages/analytics.jsx`, `pages/sites.jsx`, `components/EvidenceViewer.jsx`,
  `pages/ImportPage.jsx` (top bar), `services/api.js`.
* Route names live in `lib/routes.js`. Home is `/home` for now so your Review page at `/` is not
  touched. To make Home the front page: rename `pages/index.jsx` to `pages/review.jsx`, rename
  `pages/home.jsx` to `pages/index.jsx`, and set `home: '/'`, `review: '/review'` in routes.js.

## Put the top bar on the Review page too
In your Review page replace its header with:

```jsx
import NavBar from '../components/NavBar';
...
<NavBar current="review" />
```
