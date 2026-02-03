# Ponude Web App (skeleton)

Minimal FastAPI + Jinja2 + HTMX starter with cookie sessions and a simple login.
Designed for Render deployment.

## Local run (Windows)
1) python -m venv .venv
2) .venv\Scripts\activate
3) pip install -r requirements.txt
4) set SECRET_KEY=dev-secret
   set USER1_USERNAME=marko
   set USER1_PASSWORD=change-me
   set USER2_USERNAME=drugi
   set USER2_PASSWORD=change-me-too
5) uvicorn app.main:app --reload

Open http://127.0.0.1:8000

## Render
Build Command: pip install -r requirements.txt
Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
Env vars: SECRET_KEY, USER1_USERNAME, USER1_PASSWORD, USER2_USERNAME, USER2_PASSWORD
