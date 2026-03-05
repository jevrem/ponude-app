# BUSINESS_PRO bundle (Render) — PDV + stavke (kompatibilno)

Vraća:
- Unos stavki kroz tablicu (dodavanje/brisanje redaka)
- PDV polje + izbor "bruto/neto"
- Prikaz: međuzbroj + PDV + ukupno (u view i portalu)

Deploy:
- Zamijeni `app/` u GitHubu i push -> Render deploy

Baza:
- Aplikacija pri startu automatski doda minimalne kolone ako fale: `status`, `portal_token`, `vat_rate`, `prices_include_vat`.
