"""Chargement de la configuration : fichiers YAML versionnes + variables d'env.

Aucun seuil n'est code en dur dans les etapes. Tout ce qui pourrait avoir besoin
d'etre ajuste sans redeploiement vit ici.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class SuffixRule(BaseModel):
    value: str
    target_field: str
    extracted_value: str


class LabelSuffixConfig(BaseModel):
    suffixes: list[SuffixRule] = Field(default_factory=list)


class CategoryVat(BaseModel):
    expected: float
    tolerated: list[float] = Field(default_factory=list)

    def accepts(self, rate: float) -> bool:
        return rate == self.expected or rate in self.tolerated


class KeywordFamily(BaseModel):
    expected: float
    tokens: list[str] = Field(default_factory=list)


class VatConfig(BaseModel):
    legal_rates_fr: list[float] = Field(default_factory=list)
    expected_by_category: dict[str, CategoryVat] = Field(default_factory=dict)
    keyword_families: dict[str, KeywordFamily] = Field(default_factory=dict)

    def is_legal(self, rate: float) -> bool:
        return rate in self.legal_rates_fr


class TaxonomyConfig(BaseModel):
    paths: list[list[str]] = Field(default_factory=list)
    keywords: dict[str, list[str]] = Field(default_factory=dict)

    @cached_property
    def known_paths(self) -> set[tuple[str, ...]]:
        return {tuple(p) for p in self.paths}

    @cached_property
    def by_leaf(self) -> dict[str, tuple[str, ...]]:
        """Feuille (niveau 4) -> chemin complet.

        Sert a combler les trous « n2 vide, n3/n4 remplis » : mesure sur le
        fichier reel, aucune feuille n'est ambigue (0 conflit sur 10 000 lignes).
        """
        return {p[3]: tuple(p) for p in self.paths if len(p) == 4}

    @cached_property
    def by_level3(self) -> dict[str, tuple[str, ...]]:
        return {p[2]: tuple(p) for p in self.paths if len(p) == 4}

    @cached_property
    def canonical_by_level(self) -> list[dict[str, str]]:
        """Par niveau : forme normalisee -> orthographe de reference.

        Necessaire parce que reparer le mojibake ne suffit pas. ftfy restaure
        « Ã‰PICERIE SUCREE » en « ÉPICERIE SUCREE », avec son accent — alors que
        les 9 000 autres lignes du fichier, et donc le referentiel, ecrivent
        « EPICERIE SUCREE » sans accent. Sans cette table, la reparation
        d'encodage CREE une categorie fantome au lieu d'en supprimer une :
        mesure sur le fichier reel, 104 lignes concernees.
        """
        from pipeline.normalize import normalize_label

        levels: list[dict[str, str]] = [{}, {}, {}, {}]
        for path in self.paths:
            for index, value in enumerate(path[:4]):
                levels[index][normalize_label(value)] = value
        return levels


class Thresholds(BaseModel):
    """Seuils par champ. Deliberement separes : un taux d'EAN douteux et un taux
    d'URL morte n'ont pas la meme gravite metier."""

    quarantine_anomaly_rate: float = Field(
        default=0.60,
        description="Part de lignes portant >=1 anomalie ERROR au-dela de laquelle "
        "le run entier passe en QUARANTINE et rien n'est integre.",
    )
    max_ean_missing_rate: float = 0.30
    max_url_unreachable_rate: float = 0.30
    min_publishable_rate: float = 0.10
    # Sous ce seuil, une proposition du LLM est conservee mais NON appliquee :
    # elle part en revue humaine. Un libelle mal reecrit se voit en rayon.
    min_label_confidence: float = 0.80
    min_taxonomy_confidence: float = 0.85


class LlmConfig(BaseModel):
    """Parametres de l'etage semantique.

    Le modele par defaut est Claude Opus 5. Choisir un modele moins cher est une
    decision d'arbitrage qui appartient a l'utilisateur, pas un defaut impose :
    l'interface affiche le cout reel pour que cet arbitrage se fasse sur des
    chiffres et non a l'aveugle.
    """

    api_key: str = ""
    model: str = "claude-opus-5"
    # 273 libelles en lots de 25 = 11 appels au lieu de 273 : l'en-tete du
    # prompt est ecrit 11 fois au lieu de 273.
    batch_size: int = 25
    max_calls_per_run: int = 500
    # Plafond de depense par run, en dollars.
    max_cost_usd: float = 5.0
    # `low` suffit pour reecrire un libelle caisse et choisir dans une liste
    # fermee ; monter l'effort couterait sans rien apporter.
    effort: str = "low"


class HttpConfig(BaseModel):
    """Le profilage a montre 9 085 URLs sur seulement 6 hotes, dont 8 600 sur un
    seul (picsum.photos). Sans limite par domaine on DoS un tiers ; sans cache
    negatif au niveau du domaine on lance 443 requetes vers un domaine dont on
    sait deja que le DNS ne resout pas."""

    enabled: bool = True
    timeout_s: float = 5.0
    max_retries: int = 2
    concurrency_per_domain: int = 8
    global_concurrency: int = 32
    cache_ttl_hours: int = 24


class Settings(BaseSettings):
    """Variables d'environnement. Aucun secret n'a de valeur par defaut."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://catalog:catalog@localhost:5433/catalog"
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"
    llm_max_calls_per_run: int = 500
    llm_max_cost_usd: float = 5.0
    log_level: str = "INFO"
    log_json: bool = True
    data_dir: Path = Path("data")


class PipelineConfig(BaseModel):
    """Configuration complete, assemblee au demarrage d'un run."""

    taxonomy: TaxonomyConfig
    vat: VatConfig
    labels: LabelSuffixConfig
    thresholds: Thresholds = Field(default_factory=Thresholds)
    http: HttpConfig = Field(default_factory=HttpConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)

    @classmethod
    def load(cls, config_dir: Path | None = None, **overrides: Any) -> PipelineConfig:
        d = config_dir or CONFIG_DIR
        settings = Settings()
        llm = LlmConfig(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
            max_calls_per_run=settings.llm_max_calls_per_run,
            max_cost_usd=settings.llm_max_cost_usd,
        )
        return cls(
            taxonomy=TaxonomyConfig(**_read_yaml(d / "taxonomy.yaml")),
            vat=VatConfig(**_read_yaml(d / "vat_rules.yaml")),
            labels=LabelSuffixConfig(**_read_yaml(d / "label_suffixes.yaml")),
            llm=llm,
            **overrides,
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} ne contient pas un mapping YAML")
    return data
