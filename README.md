# Ponude – BUSINESS_PRO

Web aplikacija za izradu i upravljanje ponudama.

## Stack
- Python
- FastAPI
- PostgreSQL
- psycopg
- Jinja2 templates
- ReportLab (PDF generiranje)
- OpenPyXL (Excel export)
- Deploy: Render

## Funkcionalnosti
- dashboard ponuda
- klijenti
- kreiranje ponuda
- PDF generiranje
- portal za klijenta
- login za admina
- audit / logs
- export u Excel

## Struktura projekta

app/
  main.py
  db.py
  security.py
  templates/
  static/
  fonts/

## Environment varijable

DATABASE_URL
ADMIN_USERNAME
ADMIN_PASSWORD

## Lokalno pokretanje

pip install -r requirements.txt
uvicorn app.main:app --reload

## Deploy na Render

python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT

## Font

app/fonts/DejaVuSans.ttf
