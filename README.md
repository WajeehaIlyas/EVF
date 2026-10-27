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
docker-compose.yml   
.env.example        
```

