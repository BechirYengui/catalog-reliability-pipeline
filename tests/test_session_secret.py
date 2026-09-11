"""L'API refuse de demarrer sans cle de signature propre.

C'etait le defaut le plus grave de la couche d'authentification, et le plus
discret : sans `SESSION_SECRET`, l'API demarrait normalement, sans un log, en
signant les sessions avec `dev-only-insecure-secret` — une constante presente
dans le depot et dans l'image Docker.

A partir de la, la signature ne protege plus rien. Le jeton porte `{"sub":
..., "role": "admin"}` et n'est valide que par un HMAC dont la cle est
publique : quiconque lit le depot forge un jeton administrateur et obtient les
decisions de TVA, la suppression de runs et le referentiel de tous les
magasins.

Le controle echoue par DEFAUT plutot qu'il n'autorise par defaut, et il n'a pas
de variable d'echappement : une variable d'echappement finit toujours par etre
posee en production « juste pour debloquer ».

Le controle vit au demarrage et non a chaque signature : echouer sur une
requete laisserait un service a moitie vivant, echouer au demarrage se voit
tout de suite — le deploiement devient rouge au lieu d'exposer le referentiel
en silence.
"""

from __future__ import annotations

import pytest

from api.security import (
    DEV_FALLBACK_SECRET,
    MIN_SECRET_LENGTH,
    check_session_secret,
    issue_token,
    read_token,
)

BONNE_CLE = "9f2c" * 16  # 64 caracteres, la forme de `openssl rand -hex 32`


class TestLeDemarrageEstRefuse:
    @pytest.mark.parametrize(
        ("valeur", "cas"),
        [
            (None, "variable absente"),
            ("", "variable vide"),
            ("   ", "variable blanche"),
            (DEV_FALLBACK_SECRET, "cle de developpement publiee dans le depot"),
            ("court", "cle trop courte"),
            ("a" * (MIN_SECRET_LENGTH - 1), "un caractere sous le minimum"),
        ],
    )
    def test_une_cle_faible_empeche_le_demarrage(
        self, monkeypatch: pytest.MonkeyPatch, valeur: str | None, cas: str
    ) -> None:
        if valeur is None:
            monkeypatch.delenv("SESSION_SECRET", raising=False)
        else:
            monkeypatch.setenv("SESSION_SECRET", valeur)

        with pytest.raises(RuntimeError) as erreur:
            check_session_secret()

        message = str(erreur.value)
        assert "SESSION_SECRET" in message, f"{cas} : le message ne nomme pas la variable"
        assert "openssl rand -hex 32" in message, (
            f"{cas} : le message ne dit pas comment reparer — "
            f"une erreur doit expliquer le remede, pas seulement le probleme"
        )

    def test_une_cle_correcte_laisse_demarrer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_SECRET", BONNE_CLE)
        check_session_secret()  # ne leve pas

    def test_le_minimum_est_atteignable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La borne est inclusive : une cle d'exactement MIN_SECRET_LENGTH passe."""
        monkeypatch.setenv("SESSION_SECRET", "b" * MIN_SECRET_LENGTH)
        check_session_secret()


class TestLaSignatureResteFonctionnelle:
    """Le garde-fou ne doit pas casser ce qu'il protege."""

    def test_un_jeton_signe_se_relit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_SECRET", BONNE_CLE)
        jeton = issue_token("utilisateur-1", "admin")
        charge = read_token(jeton)

        assert charge is not None
        assert charge["sub"] == "utilisateur-1"
        assert charge["role"] == "admin"

    def test_un_jeton_signe_avec_une_autre_cle_est_rejete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C'est tout l'enjeu : un jeton forge avec la cle publiee ne doit pas
        ouvrir une session sur une instance correctement configuree."""
        monkeypatch.setenv("SESSION_SECRET", DEV_FALLBACK_SECRET)
        forge = issue_token("intrus", "admin")

        monkeypatch.setenv("SESSION_SECRET", BONNE_CLE)
        assert read_token(forge) is None, (
            "un jeton signe avec la cle de developpement est accepte par une "
            "instance qui a sa propre cle"
        )
