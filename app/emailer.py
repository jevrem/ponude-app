import os
import smtplib
from email.message import EmailMessage

# Minimal SMTP sender. Configure via env vars:
# SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
def send_offer_email(to_email: str, subject: str, body: str, pdf_bytes: bytes, filename: str = "ponuda.pdf"):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    sender = os.getenv("SMTP_FROM", user)

    if not host or not sender:
        raise RuntimeError("SMTP is not configured (set SMTP_HOST and SMTP_FROM at minimum).")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
