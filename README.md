# EVF – Events Validation Framework

Import traffic-violation events, review them (true positive / false positive / unsure), and see
analytics per site. Reviewed data can be exported as a classified zip or an `.xlsx` workbook.

## Features
- **Import data** – upload event packages per site (images + JSON), or a labelled `.xlsx`.
- **Review** – full-image viewer with a hover magnifier, keyboard shortcuts, make/model/colour checks.
- **Sites** – search and revisit every event of a site, view the evidence, delete a site.
- **Analytics** – totals, TP/FP/unsure, clear/unclear images, precision (also excluding unclear FPs),
  make/model/colour accuracy. Analytics stay available after events are removed from the site.
- **Exports** – classified zip (`TP/{clear,unclear}`, `FP/{clear,unclear}`, `Unsure`) and `.xlsx`.
- **Login** – username and password.

## Stack
FastAPI + SQLAlchemy (backend) · Next.js (pages router) + Tailwind + axios (frontend) ·
PostgreSQL (production) / SQLite (local development) · Celery for background ingestion.

## Repository layout
```
backend/    FastAPI app (app/), tests (tests/), pyproject.toml
frontend/   Next.js app (src/pages, src/components, src/store, src/services, src/lib)
docker-compose.yml   (if you use Docker)
.env.example         copy to .env and fill in
```

## Run locally
Backend:
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
export DATABASE_URL=sqlite:///./dev.db        # or your PostgreSQL URL
export EVF_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
python3 -m app.scripts.create_user <username>  # asks for a password (min. 8 characters)
uvicorn app.main:app --reload --port 8000
```
Frontend:
```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1 npm run dev
```
Open http://localhost:3000 and sign in.

With Docker, run the user script inside the backend container so it uses the real database:
`docker compose exec <backend-service> python -m app.scripts.create_user <username>`

## Configuration
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Database connection string |
| `EVF_SECRET_KEY` | Signs login tokens. Set a long random value in production |
| `EVF_TOKEN_TTL_HOURS` | Login lifetime (default 12) |
| `NEXT_PUBLIC_API_URL` | Public URL of the API, used by the browser |
| `NEXT_PUBLIC_MEDIA_URL` | Optional: where event images are served from |

## Tests
```bash
cd backend && pytest
```

## Users
There is no sign-up page. An administrator creates or resets users with
`python -m app.scripts.create_user <username>`.

## Security notes
- Sign-in uses an HttpOnly session cookie. nginx checks it (`auth_request`) before serving `/api/`, `/media/` and `/ws/`, so the API, event images and downloads all need a login. Only the page shell, the login call and the health probes are public.
- Never commit `.env`, databases or uploaded images (see `.gitignore`).
- Serve the site over HTTPS and keep the database port closed to the internet.
