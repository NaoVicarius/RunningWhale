"""Tests de la couche coach : construction du contexte et de la requête.

Aucun appel réseau : le client Anthropic est simulé. Ces tests vérifient que la
requête envoyée est correctement formée et que les cas d'erreur sont gérés.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from runningwhale import llm
from runningwhale.analysis import analyse_seance, bilan
from runningwhale.config import Athlete, Config, Race
from runningwhale.models import Activity
from runningwhale.plan import SCHEMA_PLAN


# ---- doublures ----

class BlocTexte:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FausseReponse:
    def __init__(self, texte: str = "Réponse du coach", stop_reason: str = "end_turn"):
        self.content = [BlocTexte(texte)]
        self.stop_reason = stop_reason
        self.stop_details = None


class FauxFlux:
    def __init__(self, reponse):
        self._reponse = reponse

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._reponse


class FauxMessages:
    def __init__(self, journal, reponse, exception=None):
        self._journal = journal
        self._reponse = reponse
        self._exception = exception

    def stream(self, **kwargs):
        self._journal.append(kwargs)
        if self._exception:
            raise self._exception
        return FauxFlux(self._reponse)


class FauxClient:
    """Client Anthropic simulé, qui enregistre les requêtes reçues."""

    def __init__(self, reponse=None, exception_beta=None):
        self.appels_beta: list[dict] = []
        self.appels_standard: list[dict] = []
        reponse = reponse or FausseReponse()
        self.messages = FauxMessages(self.appels_standard, reponse)
        self.beta = type(
            "Beta", (), {"messages": FauxMessages(self.appels_beta, reponse, exception_beta)}
        )()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        athlete=Athlete(
            prenom="Alex",
            sexe="H",
            fc_max=190,
            fc_repos=50,
            allure_seuil_s_km=255,
            seances_par_semaine=4,
            historique="Aponévrosite plantaire en 2025",
            objectifs=[
                Race(nom="Semi de la Ville", date=date.today() + timedelta(days=98),
                     distance_km=21.0975, objectif_temps_s=5280)
            ],
        ),
        home=tmp_path,
        db_path=tmp_path / "db.sqlite",
        reports_dir=tmp_path / "reports",
        token_dir=tmp_path / "tokens",
        config_path=tmp_path / "athlete.yml",
    )


@pytest.fixture
def activites() -> list[Activity]:
    return [
        Activity(
            activity_id=f"a{i}",
            debut=datetime.now() - timedelta(days=i * 2),
            type="running",
            nom="Footing",
            distance_m=10000,
            duree_s=3000,
            duree_mouvement_s=3000,
            fc_moy=150,
            fc_max=165,
        )
        for i in range(15)
    ]


# ---- contexte ----

def test_contexte_athlete_inclut_profil_et_objectif(cfg):
    txt = llm.contexte_athlete(cfg)
    assert "Alex" in txt
    assert "190 bpm" in txt
    assert "Semi de la Ville" in txt
    assert "Aponévrosite" in txt
    assert "semaines" in txt


def test_contexte_athlete_sans_objectif(cfg):
    cfg.athlete.objectifs = []
    assert "Aucun objectif" in llm.contexte_athlete(cfg)


def test_contexte_bilan_contient_les_metriques(cfg, activites):
    txt = llm.contexte_bilan(bilan(activites, cfg.athlete))
    assert "Charge chronique" in txt
    assert "Volumes hebdomadaires" in txt
    assert "Répartition d'intensité" in txt


def test_contexte_seance(cfg, activites):
    analyse = analyse_seance(activites[0], cfg.athlete)
    txt = llm.contexte_seance(analyse)
    assert "10.00 km" in txt
    assert "150 bpm" in txt
    assert "Charge calculée" in txt


# ---- construction de la requête ----

def test_requete_bien_formee(cfg, activites, monkeypatch):
    client = FauxClient()
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    llm.bilan_periodique(cfg, bilan(activites, cfg.athlete))

    assert len(client.appels_beta) == 1
    appel = client.appels_beta[0]
    assert appel["model"] == "claude-opus-5"
    assert appel["thinking"] == {"type": "adaptive"}
    assert appel["output_config"]["effort"] == "high"
    assert appel["fallbacks"] == "default"
    # Le prompt système est stable d'un appel à l'autre : il doit être mis en cache
    assert appel["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Alex" in appel["system"][0]["text"]


def test_plan_impose_le_schema_json(cfg, activites, monkeypatch):
    payload = (
        '{"titre":"S1","focus":"f","resume":"r","points_de_vigilance":[],'
        '"volume_total_km":40,"seances":[]}'
    )
    client = FauxClient(FausseReponse(payload))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    plan = llm.plan_semaine(cfg, bilan(activites, cfg.athlete), date.today())

    fmt = client.appels_beta[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is SCHEMA_PLAN
    assert plan.titre == "S1"


def test_plan_json_invalide_leve_une_erreur(cfg, activites, monkeypatch):
    client = FauxClient(FausseReponse("ceci n'est pas du JSON"))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    with pytest.raises(llm.CoachError, match="JSON"):
        llm.plan_semaine(cfg, bilan(activites, cfg.athlete), date.today())


def test_repli_sur_appel_standard_si_beta_refuse(cfg, activites, monkeypatch):
    # Un SDK ou une API qui ne connaît pas `fallbacks` doit faire retomber
    # l'appel sur le chemin standard plutôt que d'échouer.
    client = FauxClient(exception_beta=TypeError("unexpected keyword 'fallbacks'"))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    texte = llm.bilan_periodique(cfg, bilan(activites, cfg.athlete))

    assert len(client.appels_beta) == 1
    assert len(client.appels_standard) == 1
    assert "fallbacks" not in client.appels_standard[0]
    assert texte == "Réponse du coach"


def test_refus_du_modele_leve_une_erreur(cfg, activites, monkeypatch):
    client = FauxClient(FausseReponse("", stop_reason="refusal"))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    with pytest.raises(llm.CoachError, match="décliné"):
        llm.bilan_periodique(cfg, bilan(activites, cfg.athlete))


def test_reponse_vide_leve_une_erreur(cfg, activites, monkeypatch):
    client = FauxClient(FausseReponse("   "))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    with pytest.raises(llm.CoachError, match="aucun texte"):
        llm.bilan_periodique(cfg, bilan(activites, cfg.athlete))


def test_absence_de_cle_api_message_explicite(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.CoachError, match="ANTHROPIC_API_KEY"):
        llm._client(cfg)


def test_question_inclut_la_seance_si_demandee(cfg, activites, monkeypatch):
    client = FauxClient()
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    analyse = analyse_seance(activites[0], cfg.athlete)
    llm.question(cfg, bilan(activites, cfg.athlete), "Je suis prêt ?", analyse)

    prompt = client.appels_beta[0]["messages"][0]["content"]
    assert "Je suis prêt ?" in prompt
    assert "Charge calculée" in prompt


# ---- cas limites du coaching ----

def _acts_debutant():
    """Trois sorties sur une semaine : tout l'historique est récent."""
    return [
        Activity(
            activity_id=f"n{i}", debut=datetime.now() - timedelta(days=j),
            type="running", nom="Footing", distance_m=5000, duree_s=1700,
            duree_mouvement_s=1700, fc_moy=150, fc_max=165,
        )
        for i, j in enumerate((1, 3, 6))
    ]


def test_contexte_bilan_qualifie_l_historique_court(cfg):
    b = bilan(_acts_debutant(), cfg.athlete)
    assert b.historique_recent_court
    txt = llm.contexte_bilan(b)
    assert "Historique récent limité" in txt
    # L'ACWR sature à 4.0 quand toute la charge est récente : le contexte doit
    # le qualifier au lieu de le présenter comme une mesure.
    assert "peu fiables" in txt


def test_contexte_bilan_historique_normal_non_qualifie(cfg, activites):
    b = bilan(activites, cfg.athlete)
    assert not b.historique_recent_court
    assert "Historique récent limité" not in llm.contexte_bilan(b)


def test_contexte_bilan_sans_cardio_ne_montre_pas_de_faux_pourcentages(cfg):
    sans_fc = [
        Activity(
            activity_id=f"s{i}", debut=datetime.now() - timedelta(days=i * 2),
            type="running", nom="Footing", distance_m=10000, duree_s=3000,
            duree_mouvement_s=3000, fc_moy=None, fc_max=None,
        )
        for i in range(15)
    ]
    txt = llm.contexte_bilan(bilan(sans_fc, cfg.athlete))
    assert "Aucune donnée de fréquence cardiaque" in txt
    assert "0.0 %" not in txt


def test_plan_semaine_de_course_impose_l_affutage(cfg, activites, monkeypatch):
    payload = (
        '{"titre":"S1","focus":"f","resume":"r","points_de_vigilance":[],'
        '"volume_total_km":null,"seances":[]}'
    )
    client = FauxClient(FausseReponse(payload))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    # Course dans 3 jours : elle tombe dans la semaine planifiée.
    cfg.athlete.objectifs[0].date = date.today() + timedelta(days=3)
    llm.plan_semaine(cfg, bilan(activites, cfg.athlete), date.today())
    prompt = client.appels_beta[0]["messages"][0]["content"]
    assert "affûtage" in prompt
    assert "aucune séance dure" in prompt

    # Course dans 10 semaines : pas de consigne d'affûtage.
    cfg.athlete.objectifs[0].date = date.today() + timedelta(days=70)
    llm.plan_semaine(cfg, bilan(activites, cfg.athlete), date.today())
    assert "affûtage" not in client.appels_beta[1]["messages"][0]["content"]


def test_plan_semaine_attache_les_avertissements(cfg, activites, monkeypatch):
    jour_passe = (date.today() - timedelta(days=30)).isoformat()
    payload = (
        '{"titre":"S1","focus":"f","resume":"r","points_de_vigilance":[],'
        '"volume_total_km":null,"seances":[{"date":"' + jour_passe + '",'
        '"titre":"Footing","type":"endurance","distance_km":8,"duree_min":45,'
        '"objectif":"o","consignes":"c","etapes":[]}]}'
    )
    client = FauxClient(FausseReponse(payload))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    plan = llm.plan_semaine(cfg, bilan(activites, cfg.athlete), date.today())
    assert any("hors de la semaine" in a for a in plan.avertissements)


def test_semaine_de_course_neutralise_les_cibles_hebdo(cfg, activites, monkeypatch):
    payload = (
        '{"titre":"S1","focus":"f","resume":"r","points_de_vigilance":[],'
        '"volume_total_km":null,"seances":[]}'
    )
    client = FauxClient(FausseReponse(payload))
    monkeypatch.setattr(llm, "_client", lambda _cfg: client)

    cfg.athlete.volume_hebdo_cible_km = 45.0
    cfg.athlete.objectifs[0].date = date.today() + timedelta(days=3)
    llm.plan_semaine(cfg, bilan(activites, cfg.athlete), date.today())
    prompt = client.appels_beta[0]["messages"][0]["content"]
    # Les cibles habituelles contrediraient l'affûtage : elles sont retirées.
    assert "Vise 4 séances" not in prompt
    assert "Volume hebdomadaire cible" not in prompt
