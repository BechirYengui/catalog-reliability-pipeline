"""Mots de passe et jetons de session.

Volontairement en bibliotheque standard : PBKDF2-HMAC-SHA256 est dans `hashlib`
et le jeton signe tient en quelques lignes de `hmac`. Ajouter passlib ou
python-jose apporterait deux dependances de plus a maintenir et a auditer pour
un besoin que la stdlib couvre correctement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

# Cout de derivation. 600 000 iterations est la recommandation OWASP 2023 pour
# PBKDF2-HMAC-SHA256 ; sur ce VPS cela represente ~200 ms par verification,
# ce qui est le bon ordre de grandeur pour une page de connexion.
_ITERATIONS = 600_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != _ALGO:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Comparaison a temps constant : une comparaison naive fuit la longueur du
    # prefixe correct et permet de reconstruire le hash octet par octet.
    return hmac.compare_digest(digest.hex(), digest_hex)


# Repli de developpement. Cette chaine est dans le depot et dans l'image
# Docker : quiconque la lit peut signer un jeton `admin` valide. Elle ne doit
# jamais servir ailleurs qu'en local, et `check_session_secret()` refuse le
# demarrage si elle est en usage.
DEV_FALLBACK_SECRET = "dev-only-insecure-secret"

# `openssl rand -hex 32` en produit 64. On refuse en dessous de 32 : une cle
# courte se retrouve par force brute, et la signature ne protege alors plus le
# ROLE porte par le jeton.
MIN_SECRET_LENGTH = 32


def _secret() -> bytes:
    key = os.environ.get("SESSION_SECRET", "")
    if not key:
        # Tolere ici pour que les tests et le developpement local tournent sans
        # ceremonie. Le refus est au DEMARRAGE (`check_session_secret`), pas a
        # chaque signature : echouer sur une requete laisserait un service
        # a moitie vivant, echouer au demarrage se voit tout de suite.
        key = DEV_FALLBACK_SECRET
    return key.encode()


def check_session_secret() -> None:
    """Refuse de demarrer sans cle de signature propre.

    Le repli silencieux etait le defaut le plus grave de cette couche : sans
    `SESSION_SECRET`, l'API demarrait normalement, sans un log, en signant les
    sessions avec une constante publiee dans le depot. A partir de la,
    n'importe qui forge un jeton `admin` — donc les decisions de TVA, la
    suppression de runs et le referentiel entier de tous les magasins.

    Le controle echoue par DEFAUT plutot qu'il n'autorise par defaut : il n'y a
    pas de variable d'echappement, parce qu'une variable d'echappement finit
    toujours par etre posee en production « juste pour debloquer ».
    """
    key = os.environ.get("SESSION_SECRET", "").strip()
    remede = (
        "Generer une cle : openssl rand -hex 32, puis la placer dans SESSION_SECRET (fichier .env)."
    )
    if not key:
        raise RuntimeError(
            "SESSION_SECRET est absente : les sessions seraient signees avec la "
            f"cle de developpement, publiee dans le depot. {remede}"
        )
    if key == DEV_FALLBACK_SECRET:
        raise RuntimeError(
            "SESSION_SECRET vaut la cle de developpement, qui est publiee dans "
            f"le depot et permet de forger un jeton admin. {remede}"
        )
    if len(key) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"SESSION_SECRET fait {len(key)} caracteres, minimum {MIN_SECRET_LENGTH}. {remede}"
        )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(user_id: str, role: str, ttl_seconds: int = 86_400 * 7) -> str:
    payload = {"sub": user_id, "role": role, "exp": int(time.time()) + ttl_seconds}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def read_token(token: str) -> dict[str, Any] | None:
    """Retourne la charge utile si la signature est valide et le jeton non expire.

    Le type annonce `Any` en valeur, et non `str` : la charge porte `exp`, qui
    est un entier. L'annoncer `dict[str, str]` etait faux, et cachait que le
    contenu vient d'un `json.loads` — donc de l'exterieur, meme si la signature
    garantit qu'il n'a pas ete altere.
    """
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64(expected), signature):
        return None

    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None

    # Un jeton signe portant autre chose qu'un objet (`"[1,2]"`, `"null"`)
    # passerait la signature et ferait tomber l'appelant sur `payload["sub"]`.
    if not isinstance(payload, dict):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
