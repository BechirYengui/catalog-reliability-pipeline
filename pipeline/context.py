"""Contexte d'un run : identite, configuration, horloge.

L'horloge est injectable pour que les tests puissent produire des `Correction`
horodatees de facon deterministe — sans quoi comparer deux rapports devient
impossible.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.steps.url_checker import UrlStatus


def compute_file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Hash du fichier source. C'est la cle d'idempotence : rejouer exactement le
    meme fichier pour le meme magasin ne doit rien creer de nouveau."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class RunContext:
    store_id: str
    source_file: Path
    config: PipelineConfig
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    file_sha256: str = ""
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    network_enabled: bool = True
    # Statuts d'URL etablis par les runs precedents (charges de la base par
    # l'orchestrateur, TTL deja applique) et ceux que CE run etablit par le
    # reseau. Deux champs plutot qu'un : ce qui se lit et ce qui se persiste
    # ne se recouvrent pas, et les confondre referait verifier ou reecrire
    # les 9 000 memes URLs chaque matin.
    url_status_seed: dict[str, UrlStatus] = field(default_factory=dict)
    url_status_fresh: dict[str, UrlStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.file_sha256 and self.source_file.exists():
            self.file_sha256 = compute_file_sha256(self.source_file)

    def now(self) -> datetime:
        return self.clock()
