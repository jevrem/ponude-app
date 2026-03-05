# BUSINESS_PRO bundle (Render) — kompatibilno s postojećom bazom

Ovaj bundle je napravljen da radi i s:
- novim schema (offers.client_id -> clients)
- starim schema (offers.client_oib i/ili client_* polja na offers)

Dodano:
- `/offer` -> redirect na `/` (da stari linkovi ne pucaju)

Napomena:
- Ako u tvojoj bazi NE postoji `offers.status`, onda prihvaćanje ponude neće raditi dok ne dodaš status u bazu.
- Ako želiš puni novi schema (statusi, counters, relacije), pokreni `app/schema.sql` (jednom).
