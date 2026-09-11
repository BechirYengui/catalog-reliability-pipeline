# Image du pipeline.
#
# Construite dans GitHub Actions et poussee sur GHCR ; le VPS ne fait qu'un
# `docker pull`. C'est une deviation assumee par rapport au CI/CD des autres
# sites du serveur, qui construit sur place : ici le pipeline embarquera torch (~800 Mo) a la
# phase 2, et construire ca sur 2 vCPU pendant que trois autres services tournent
# reproduirait la saturation disque du 13/05.
#
# Multi-stage : les outils de build ne partent pas en production.

FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app

# Les dependances d'abord, le code ensuite : tant que uv.lock ne bouge pas,
# cette couche reste en cache et un deploiement ne reconstruit que le code.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY pipeline/ ./pipeline/
COPY api/ ./api/
COPY db/ ./db/
COPY config/ ./config/
# alembic.ini est indispensable au demarrage : sans lui, migrate() ne trouve
# pas les revisions et le conteneur demarre sur un schema absent.
COPY alembic.ini ./
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

# Utilisateur non privilegie : le conteneur n'a aucune raison de tourner en root.
RUN groupadd --system --gid 1001 catalog \
    && useradd --system --uid 1001 --gid catalog --create-home catalog

WORKDIR /app

COPY --from=builder --chown=catalog:catalog /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Les repertoires de travail sont montes depuis l'hote (/srv/catalog).
RUN mkdir -p /data/inbox /data/processed /data/quarantine \
    && chown -R catalog:catalog /data

USER catalog

# Une seule image pour le pipeline et l'API : meme code metier, memes
# dependances. Deux images, ce serait deux builds et le risque de les voir
# diverger. Le role est choisi par la commande, dans le compose.
ENTRYPOINT []
CMD ["pipeline", "--help"]
