# BUSINESS_PRO bundle (Render)

## Što je unutra
- `app/` FastAPI aplikacija
- `app/schema.sql` baza (includes `offers.status`)
- Jinja templates + static CSS
- ReportLab PDF s DejaVuSans fontom (za hrvatska slova)

## Render
- Postavi env var `DATABASE_URL` (Render Postgres ga automatski daje)
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Baza
Pokreni `app/schema.sql` na Render Postgresu (jednom).

## Portal prihvaćanja ponude
- `/p/{token}` prikaz ponude
- `/p/{token}/accept` postavlja status na `accepted`
