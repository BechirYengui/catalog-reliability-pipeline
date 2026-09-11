"""Logs structures. Le funnel du pipeline doit se lire directement dans les logs :
combien de lignes entrent et sortent de chaque etape, combien de paires restent a
comparer, combien d'appels LLM sont reels et combien viennent du cache.

Le `run_id` est lie au contexte (contextvars) : toute ligne emise pendant un run
le porte, sans avoir a le passer explicitement partout.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    # httpx journalise CHAQUE requete en INFO. Sur un run reel c'est ~9 000
    # lignes qui noient le funnel et remplissent journald sur un VPS modeste.
    # Ce qui nous interesse est deja agrege dans l'evenement `url.check_done`.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_run(run_id: str, store_id: str) -> None:
    structlog.contextvars.bind_contextvars(run_id=run_id, store_id=store_id)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()
