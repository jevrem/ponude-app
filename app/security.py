import secrets
import hashlib

def new_portal_token(nbytes: int = 18) -> str:
    # URL-safe token
    return secrets.token_urlsafe(nbytes)

def hash_token(token: str) -> str:
    # Optional if you want to store hashes; currently not used by default schema
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
