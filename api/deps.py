"""Dependances FastAPI : session de base et utilisateur courant."""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.security import read_token
from db.models import User
from db.session import get_session

SESSION_COOKIE = "catalog_session"

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def current_user(
    session: DbSession,
    catalog_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    if not catalog_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentification requise")

    payload = read_token(catalog_session)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session invalide ou expiree")

    user = await session.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "compte inconnu ou desactive")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    """Valider un taux de TVA n'est pas une action anodine.

    Le role est verifie cote serveur : masquer un bouton dans l'interface ne
    protege rien, l'appel HTTP reste faisable a la main.
    """
    if user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cette decision engage la responsabilite fiscale et demande un role admin",
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def get_or_create_user(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()
