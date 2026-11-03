# EVF – Events Validation Framework

Import traffic-violation events, review them (true positive / false positive / unsure), and see
analytics per site. Reviewed data can be exported as a classified zip or an `.xlsx` workbook.

## Features
- **Login** – username and password; signing in opens the Home page.
- **Home** – plain page linking to Sites, Analytics, Import data and Review.
- **Import data** – upload event packages per site (images + JSON), or a labelled `.xlsx`.
- **Review** – full-image viewer with a hover magnifier, keyboard shortcuts, make/model/colour checks.
  Choosing a site selects a feature that still has unreviewed events.
- **Sites** – search and revisit every event of a site, view the evidence, download the site as
  `.xlsx`, jump to its unreviewed events, delete a site.
- **Analytics** – totals, TP/FP/unsure, clear/unclear images, precision (also excluding unclear FPs),
  make/model/colour accuracy, per site and per feature. Available at any time, even mid-review, and
  they stay after events are removed from the site.
- **Exports** – classified zip (`TP/{clear,unclear}`, `FP/{clear,unclear}`, `Unsure`)

## Stack
FastAPI + SQLAlchemy · Next.js (pages router) + Tailwind + axios · PostgreSQL 15 · Redis + Celery
(background imports, folder watcher, scheduled jobs) · nginx reverse proxy · Docker Compose.

## Repository layout
```
backend/             FastAPI app (app/), tests (tests/), Dockerfile, pyproject.toml
frontend/            Next.js app (src/pages, src/components, src/store, src/services, src/lib)
nginx/nginx.conf     reverse proxy: routing, rate limits, login check for /api, /media, /ws
docker-compose.yml   the whole stack
.env                 settings and passwords
secrets/             credential files mounted into the backend
data/                hot / ingest input folders
```

## Security
- Sign-in sets an HttpOnly session cookie. nginx checks it (`auth_request`) before serving `/api/`,
  `/media/` and `/ws/`, so the API, event images and downloads all need a login. Only the page
  shell, the sign-in call and the health probes are public. Sign-in is rate limited, and a username
  is locked out for a few minutes after 8 wrong passwords.
- Serve the site over HTTPS (terminate TLS in front of nginx, e.g. Caddy or a load balancer, and
  keep `X-Forwarded-Proto`); the cookie then becomes HTTPS-only automatically.
- Only nginx is published; the database and Redis are not reachable from outside Docker.

