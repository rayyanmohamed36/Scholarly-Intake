# Scholarly Intake

## Introduction
Scholarly Intake streamlines how universities, research labs, and student-run journals collect scholarly work. Instead of forwarding PDFs through email threads or juggling shared drives, contributors upload their manuscripts through a guided form while admins evaluate every submission inside a single dashboard. MongoDB Atlas + GridFS keeps metadata and large PDFs together, so reviewers can approve or reject drafts with one click and readers can download the final versions straight from the platform. Whether you are launching a lightweight publishing pipeline, building an internal knowledge base, or prototyping a submission portal for conferences, this repo gives you an opinionated, secure starting point that runs anywhere FastAPI and MongoDB are available.

A FastAPI-powered content pipeline for collecting, reviewing, and publishing academic articles. This public-friendly build connects to MongoDB Atlas, stores PDFs in GridFS, and exposes both a public upload form and password-protected admin tools for screening submissions before they are made available to readers.

## Features
- Public upload endpoint with PDF validation and metadata capture (title, author, abstract, body).
- MongoDB Atlas persistence layer with GridFS-backed PDF storage.
- Admin dashboard for viewing, approving, editing, and deleting submissions.
- Secure admin authentication via signed cookies and bcrypt-hashed credentials.
- Download endpoints that stream PDFs from GridFS while protecting originals.
- Health-check route and Render-ready `Procfile` for simple deployments.

## Tech Stack
- FastAPI + Starlette middleware for the web tier.
- MongoDB Atlas with GridFS for structured data and large binary storage.
- Jinja2 templates for the upload form and admin UI.
- Uvicorn ASGI server + Procfile for Render/Heroku-style hosting.

## Project Layout
```
app.py                # FastAPI application
requirements.txt      # Python dependencies
Procfile              # Render/Heroku web process command
templates/            # Jinja2 templates (upload + admin views)
README.md             # Project documentation (this file)
.env.example          # Sample environment configuration
```

## Prerequisites
- Python 3.9+ (3.11 recommended).
- MongoDB Atlas cluster with TLS enabled.
- Access to create at least one admin user document in the `users` collection.

## Getting Started
1. **Clone & Install**
   ```bash
   git clone <repo-url>
   cd Scholarly\ Intake-public
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Configure Environment**
   ```bash
   cp .env.example .env
   ```
   Update the values (see [Configuration](#configuration)).
3. **Run Locally**
   ```bash
   uvicorn app:app --reload
   ```
   Visit `http://127.0.0.1:8000/upload` for the public form or `http://127.0.0.1:8000/admin/login` for the dashboard.

## Configuration
All secrets live in `.env` (loaded via `python-dotenv`). The sample `.env.example` includes:

| Variable | Description |
| --- | --- |
| `MONGO_URI` | Full MongoDB Atlas connection string with credentials and replica info. |
| `MONGO_DB_NAME` | Database name (defaults to `article_manager`). |
| `GRIDFS_BUCKET_NAME` | GridFS bucket where PDFs are stored (defaults to `article_pdfs`). |
| `SECRET_KEY` | Random string used to sign admin session cookies. |
| `SESSION_COOKIE_SECURE` | `true` in production to force HTTPS-only cookies. |

If any of the required values are missing the app will exit at startup.

## Creating an Admin User
There is no built-in registration flow. Seed an admin manually once per environment:
```python
from pymongo import MongoClient
import bcrypt, os
client = MongoClient(os.environ["MONGO_URI"], tls=True)
db = client[os.environ.get("MONGO_DB_NAME", "article_manager")]
password_hash = bcrypt.hashpw(b"<plain-text-password>", bcrypt.gensalt())
db.users.insert_one({
    "email": "admin@example.com",
    "password_hash": password_hash,
    "role": "admin"
})
```
Use the same email/password on `/admin/login`.

## Key Endpoints
| Path | Method | Description | Auth |
| --- | --- | --- | --- |
| `/upload` | GET | Public upload form (template). | None |
| `/upload-article` | POST | Accepts article metadata + PDF. | None |
| `/articles` | GET | Lists approved articles with metadata + GridFS IDs. | None |
| `/pdf/{file_id}` | GET | Streams a PDF from GridFS. | None |
| `/admin/login` | GET/POST | Admin authentication view + handler. | Public form, secured submission |
| `/admin/dashboard` | GET | Overview of submissions and actions. | Admin cookie |
| `/admin/upload-article` | POST | Admin-side upload. | Admin cookie |
| `/admin/view/{id}` | GET | Read-only article detail. | Admin cookie |
| `/admin/edit/{id}` | GET/POST | Edit metadata. | Admin cookie |
| `/admin/approve-article` | POST | Mark as approved. | Admin cookie |
| `/admin/delete-article` | POST | Remove article + PDF. | Admin cookie |
| `/health` | GET | Deployment readiness probe. | None |

## Deployment Notes
- Render/Heroku: keep `Procfile` (`web: uvicorn app:app --host 0.0.0.0 --port 10000`). Update the service port/environment to match your platform.
- Ensure `SESSION_COOKIE_SECURE=true` and use HTTPS in production.
- Provision MongoDB Atlas IP access for your hosting provider.
- Configure persistent storage limits if hosting large PDFs.

## Operational Tips
- GridFS cleanup: deleting an article removes its PDF via `gridfs_bucket.delete`. Monitor orphaned files if manual DB edits occur.
- Session tokens expire after 4 hours. Clearing cookies forces re-login.
- Use the `/health` endpoint for Render readiness/liveness checks.
- When scaling horizontally, reuse the same `SECRET_KEY` so existing sessions stay valid.

## Support & Contributions
Issues and suggestions are welcome via GitHub issues/PRs. Please redact secrets before sharing logs.
