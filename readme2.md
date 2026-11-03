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
- Never commit `.env`, databases or uploaded images (see `.gitignore`).
