import os
from datetime import date
from typing import List, Dict, Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_NAME = "DejaVuSans"
_font_registered = False

def _register_font():
    global _font_registered
    if _font_registered:
        return
    font_path = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
    if os.path.exists(font_path):
        pdfmetrics.registerFont(TTFont(FONT_NAME, font_path))
        _font_registered = True
    else:
        # Fallback (Croatian chars may not render)
        _font_registered = True

def build_offer_pdf(output_path: str, offer: Dict[str, Any], client: Dict[str, Any], items: List[Dict[str, Any]]):
    _register_font()

    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    # Font
    try:
        c.setFont(FONT_NAME, 12)
    except Exception:
        c.setFont("Helvetica", 12)

    y = height - 60
    c.drawString(40, y, f"PONUDA #{offer['number']}")
    y -= 24
    c.drawString(40, y, f"Datum: {offer.get('created_at', date.today().isoformat())}")
    y -= 30

    c.drawString(40, y, "Klijent:")
    y -= 18
    c.drawString(60, y, client.get("name", ""))
    y -= 16
    if client.get("address"):
        c.drawString(60, y, client["address"]); y -= 16
    if client.get("email"):
        c.drawString(60, y, client["email"]); y -= 16
    if client.get("oib"):
        c.drawString(60, y, f"OIB: {client['oib']}"); y -= 16

    y -= 10
    c.drawString(40, y, "Stavke:")
    y -= 20

    # Table headers
    c.drawString(40, y, "Opis")
    c.drawString(340, y, "Količina")
    c.drawString(430, y, "Cijena")
    y -= 14
    c.line(40, y, width - 40, y)
    y -= 18

    total = 0.0
    for it in items:
        desc = it.get("description", "")
        qty = float(it.get("qty", 0))
        price = float(it.get("unit_price", 0))
        line_total = qty * price
        total += line_total

        # Wrap description roughly
        max_len = 60
        lines = [desc[i:i+max_len] for i in range(0, len(desc), max_len)] or [""]
        c.drawString(40, y, lines[0])
        c.drawRightString(400, y, f"{qty:g}")
        c.drawRightString(width - 40, y, f"{line_total:,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
        y -= 16

        for extra in lines[1:]:
            c.drawString(40, y, extra)
            y -= 16

        if y < 120:
            c.showPage()
            try:
                c.setFont(FONT_NAME, 12)
            except Exception:
                c.setFont("Helvetica", 12)
            y = height - 60

    y -= 10
    c.line(40, y, width - 40, y)
    y -= 22
    c.drawRightString(width - 40, y, f"UKUPNO: {total:,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))

    y -= 30
    c.setFontSize(10)
    c.drawString(40, y, "Napomena: Ponuda vrijedi 15 dana, osim ako nije drugačije navedeno.")
    c.save()

    return total
