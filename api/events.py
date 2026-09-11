"""Diffusion de la progression d'un run vers les navigateurs connectes.

Un run complet dure de 2 secondes (hors reseau) a 4 minutes (avec verification
des 9 000 URLs). Faire attendre l'interface jusqu'a la fin serait une mauvaise
experience et masquerait justement ce qu'on veut montrer : ou en est le fichier.

Implementation volontairement simple — un dictionnaire de files d'attente en
memoire. Le service tourne en un seul processus sur un VPS a 2 vCPU ; ajouter
Redis pour diffuser des evenements entre des workers qui n'existent pas serait
un systeme de plus a operer pour rien. Le jour ou l'API passe a plusieurs
workers, c'est ce module, et lui seul, qu'il faut remplacer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import Any

# run_id -> files d'attente des clients abonnes
_subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

# Dernier evenement connu par run : un client qui se connecte en cours de route
# recoit immediatement l'etat courant au lieu d'un ecran vide.
_last: dict[str, str] = {}


def publish(run_id: str, payload: dict[str, Any]) -> None:
    message = json.dumps(payload, ensure_ascii=False, default=str)
    _last[run_id] = message
    for queue in list(_subscribers.get(run_id, ())):
        # `put_nowait` plutot qu'`await` : la publication est appelee depuis le
        # thread du pipeline, elle ne doit jamais bloquer le traitement parce
        # qu'un navigateur lit lentement.
        # File bornee : si un navigateur lit trop lentement, on prefere perdre
        # un evenement de progression plutot que ralentir le pipeline. L'etat
        # complet est de toute facon relu depuis l'API a chaque evenement.
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(message)


def subscribe(run_id: str) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    _subscribers[run_id].add(queue)
    if run_id in _last:
        queue.put_nowait(_last[run_id])
    return queue


def unsubscribe(run_id: str, queue: asyncio.Queue[str]) -> None:
    _subscribers.get(run_id, set()).discard(queue)
    if not _subscribers.get(run_id):
        _subscribers.pop(run_id, None)


def forget(run_id: str) -> None:
    _last.pop(run_id, None)
