"""Tests du rendu Markdown."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from runningwhale.analysis import bilan
from runningwhale.config import Athlete, Config, Race
from runningwhale.models import Activity
from runningwhale.plan import Plan
from runningwhale.report import (
    barre,
    nom_fichier,
    rapport_bilan,
    rapport_plan,
    slug,
    _ligne_volume,
)

PAYLOAD_BASE = {
    "titre": "Semaine test",
    "focus": "Endurance",
    "resume": "Résumé.",
    "points_de_vigilance": [],
    "volume_total_km": None,
    "seances": [
        {
            "date": "2026-08-17",
            "titre": "Footing",
            "type": "endurance",
            "distance_km": 10.0,
            "duree_min": 55.0,
            "objectif": "Volume",
            "consignes": "Tranquille.",
            "etapes": [],
        },
        {
            "date": "2026-08-19",
            "titre": "Seuil",
            "type": "seuil",
            "distance_km": 12.0,
            "duree_min": 60.0,
            "objectif": "Seuil",
            "consignes": "Contrôlé.",
            "etapes": [],
        },
    ],
}


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        athlete=Athlete(
            prenom="Test",
            fc_max=190,
            fc_repos=50,
            objectifs=[
                Race(nom="Semi", date=date.today() + timedelta(days=70),
                     distance_km=21.0975, objectif_temps_s=5280)
            ],
        ),
        home=tmp_path,
        db_path=tmp_path / "db.sqlite",
        reports_dir=tmp_path / "reports",
        token_dir=tmp_path / "tokens",
        config_path=tmp_path / "athlete.yml",
    )


def test_slug():
    assert slug("Sortie longue — dimanche !") == "sortie-longue-dimanche"
    assert slug("") == "rapport"
    assert slug("Éch. 10×400m") == "ech-10400m"


def test_barre():
    assert barre(0, 10) == "░" * 10
    assert barre(100, 10) == "█" * 10
    assert len(barre(37, 20)) == 20


def test_nom_fichier():
    assert nom_fichier("plan", date(2026, 8, 17)) == "2026-08-17-plan.md"
    assert nom_fichier("debrief", date(2026, 8, 17), "Sortie longue") == (
        "2026-08-17-debrief-sortie-longue.md"
    )


def test_nom_fichier_distingue_deux_activites_homonymes_le_meme_jour():
    # Avec les noms Garmin par défaut (« Course à pied »), deux sorties le même
    # jour donnaient le même nom de fichier : le second débrief écrasait le
    # premier en silence. L'heure de départ entre donc dans le nom.
    matin = nom_fichier("debrief", datetime(2026, 8, 17, 8, 30), "Course à pied")
    soir = nom_fichier("debrief", datetime(2026, 8, 17, 19, 5), "Course à pied")
    assert matin == "2026-08-17-0830-debrief-course-a-pied.md"
    assert soir == "2026-08-17-1905-debrief-course-a-pied.md"
    assert matin != soir

    # Re-débriefer la même activité réutilise le même fichier (idempotent).
    assert matin == nom_fichier("debrief", datetime(2026, 8, 17, 8, 30), "Course à pied")


def test_ligne_volume_coherente():
    plan = Plan.from_payload({**PAYLOAD_BASE, "volume_total_km": 22.0})
    ligne = _ligne_volume(plan)
    assert "22 km · 2 séances" in ligne
    assert "⚠️" not in ligne


def test_ligne_volume_signale_incoherence():
    # Le coach annonce 42 km, les séances en totalisent 22
    plan = Plan.from_payload({**PAYLOAD_BASE, "volume_total_km": 42.0})
    ligne = _ligne_volume(plan)
    assert "22 km" in ligne
    assert "⚠️" in ligne and "42" in ligne


def test_ligne_volume_sans_declaration():
    plan = Plan.from_payload(PAYLOAD_BASE)
    assert "⚠️" not in _ligne_volume(plan)


def test_rapport_plan_contient_les_seances(cfg):
    plan = Plan.from_payload(PAYLOAD_BASE)
    md = rapport_plan(cfg, plan)
    assert "# Semaine test" in md
    assert "Lundi 17/08 — Footing" in md
    assert "Mercredi 19/08 — Seuil" in md


def test_rapport_bilan_sans_activite_de_reference(cfg):
    activites = [
        Activity(
            activity_id=f"a{i}",
            debut=datetime.now() - timedelta(days=i),
            type="running",
            distance_m=7000,
            duree_s=2400,
            duree_mouvement_s=2400,
            fc_moy=140,
        )
        for i in range(10)
    ]
    md = rapport_bilan(cfg, bilan(activites, cfg.athlete))
    assert "# Bilan d'entraînement — Test" in md
    # Aucune sortie ne tombe sur une distance de référence
    assert "Aucune sortie ne correspond" in md


def test_rapport_bilan_affiche_objectif(cfg):
    activites = [
        Activity(
            activity_id="a",
            debut=datetime.now(),
            type="running",
            distance_m=10000,
            duree_s=2520,
            duree_mouvement_s=2520,
            fc_moy=170,
        )
    ]
    md = rapport_bilan(cfg, bilan(activites, cfg.athlete))
    assert "**Semi**" in md
    assert "semaines" in md


# ---- avertissements et historique court ----

def test_rapport_plan_affiche_les_avertissements(cfg):
    plan = Plan.from_payload(PAYLOAD_BASE)
    plan.avertissements = ["la séance « Footing » est datée hors de la semaine demandée"]
    md = rapport_plan(cfg, plan)
    assert "⚠️" in md
    assert "hors de la semaine demandée" in md


def test_tableau_forme_qualifie_l_historique_court(cfg):
    from runningwhale.report import tableau_forme

    recentes = [
        Activity(
            activity_id=f"n{i}", debut=datetime.now() - timedelta(days=j),
            type="running", nom="Footing", distance_m=5000, duree_s=1700,
            duree_mouvement_s=1700, fc_moy=150, fc_max=165,
        )
        for i, j in enumerate((1, 3, 6))
    ]
    b = bilan(recentes, cfg.athlete)
    md = tableau_forme(b)
    assert "Historique récent limité" in md
