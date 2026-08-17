"""Tests du serveur MCP.

Les fonctions décorées par `@serveur.tool()` restent appelables directement :
on les teste sans passer par le transport stdio, qui serait lent et n'apporterait
rien de plus ici. Un test vérifie tout de même que les outils sont bien déclarés
auprès du serveur MCP.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("mcp")

from runningwhale import mcp_server
from runningwhale.config import Athlete, Config, Race
from runningwhale.db import Database
from runningwhale.models import Activity, Lap
from runningwhale.plan import Plan
from runningwhale.wellness import Wellness

PAYLOAD_PLAN = {
    "titre": "Semaine test",
    "focus": "Endurance",
    "resume": "Résumé.",
    "points_de_vigilance": ["Attention au genou"],
    "volume_total_km": 22.0,
    "seances": [
        {
            "date": date.today().isoformat(),
            "titre": "Footing",
            "type": "endurance",
            "distance_km": 10.0,
            "duree_min": 55.0,
            "objectif": "Volume",
            "consignes": "Tranquille.",
            "etapes": [],
        },
        {
            "date": (date.today() + timedelta(days=2)).isoformat(),
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
def contexte(tmp_path, monkeypatch):
    """Configuration et base peuplées, injectées dans le serveur MCP."""
    cfg = Config(
        athlete=Athlete(
            prenom="Alex",
            sexe="H",
            fc_max=190,
            fc_repos=48,
            allure_seuil_s_km=255,
            vma_kmh=16.5,
            objectifs=[
                Race(nom="Semi", date=date.today() + timedelta(days=60),
                     distance_km=21.0975, objectif_temps_s=5280)
            ],
        ),
        home=tmp_path,
        db_path=tmp_path / "db.sqlite",
        reports_dir=tmp_path / "reports",
        token_dir=tmp_path / "tokens",
        config_path=tmp_path / "athlete.yml",
    )
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)

    for i in range(20):
        laps = [
            Lap(index=j, distance_m=1000, duree_s=300, fc_moy=145 + j, allure_s_km=300)
            for j in range(1, 9)
        ]
        db.upsert_activity(
            Activity(
                activity_id=f"a{i}",
                debut=datetime.now() - timedelta(days=i * 2, hours=3),
                type="running",
                nom=f"Sortie {i}",
                distance_m=8000,
                duree_s=2400,
                duree_mouvement_s=2400,
                fc_moy=150,
                fc_max=168,
                laps=laps if i == 0 else [],
            )
        )

    for i in range(10):
        db.upsert_wellness(
            Wellness(
                jour=date.today() - timedelta(days=i),
                sommeil_s=7.5 * 3600,
                vfc_ms=60.0,
                vfc_baseline_bas=52.0,
                fc_repos=48,
                readiness_score=72,
                readiness_niveau="élevée",
            )
        )

    monkeypatch.setattr(mcp_server, "_config", cfg)
    monkeypatch.setattr(mcp_server, "_db", db)
    return cfg, db


# ---- déclaration des outils ----

def test_les_outils_sont_declares():
    noms = {t.name for t in mcp_server.serveur._tool_manager.list_tools()}
    attendus = {
        "etat_de_forme",
        "bilan_complet",
        "recuperation",
        "activites_recentes",
        "detail_seance",
        "allures",
        "plan_en_cours",
        "synchroniser",
        "enregistrer_plan",
    }
    assert attendus <= noms


def test_le_serveur_porte_des_instructions():
    # Le modèle connecté doit savoir qu'il ne recalcule pas les métriques lui-même
    assert "calculées localement" in mcp_server.INSTRUCTIONS
    assert "recuperation" in mcp_server.INSTRUCTIONS


# ---- outils de lecture ----

def test_etat_de_forme(contexte):
    res = mcp_server.etat_de_forme()
    assert res["athlete"] == "Alex"
    assert res["ctl_condition_de_fond"] > 0
    assert "lecture_fraicheur" in res
    assert res["objectif"]["nom"] == "Semi"


def test_bilan_complet(contexte):
    res = mcp_server.bilan_complet(semaines=4)
    assert len(res["semaines"]) == 4
    assert res["vma_kmh"] == 16.5
    assert res["allures"]
    assert res["recuperation"]["disponible"] is True


def test_recuperation(contexte):
    res = mcp_server.recuperation(jours=7)
    assert res["synthese"]["jours_couverts"] > 0
    assert res["synthese"]["sommeil_moyen_h"] == 7.5
    assert len(res["jours"]) > 0
    assert res["jours"][0]["readiness_score"] == 72


def test_activites_recentes(contexte):
    res = mcp_server.activites_recentes(jours=10)
    assert len(res) > 0
    assert res[0]["distance_km"] == 8.0
    assert res[0]["allure"].endswith("/km")
    assert res[0]["charge"] > 0


def test_activites_recentes_respecte_la_limite(contexte):
    assert len(mcp_server.activites_recentes(jours=90, limite=3)) == 3


def test_detail_seance_derniere_par_defaut(contexte):
    res = mcp_server.detail_seance()
    assert res["activity_id"] == "a0"
    assert len(res["tours"]) == 8
    # La FC monte tour après tour à allure constante : dérive détectable
    assert res["decouplage_pct"] is not None


def test_detail_seance_par_identifiant(contexte):
    assert mcp_server.detail_seance("a5")["activity_id"] == "a5"


def test_detail_seance_inconnue_leve_une_erreur(contexte):
    with pytest.raises(ValueError, match="introuvable"):
        mcp_server.detail_seance("inexistante")


def test_allures(contexte):
    res = mcp_server.allures()
    assert res["vma_kmh"] == 16.5
    assert "Allure marathon (~80 % VMA)" in res["allures"]


def test_plan_absent(contexte):
    assert mcp_server.plan_en_cours()["plan"] is None


def test_plan_corrompu_ne_casse_pas_l_outil(contexte):
    # Une entrée abîmée en base ne doit pas faire planter le serveur MCP.
    _, db = contexte
    db.set_meta("plan_courant", '{"seances": [42]}')
    res = mcp_server.plan_en_cours()
    assert res["plan"] is None
    assert "illisible" in res["message"]


# ---- outil d'écriture ----

def test_enregistrer_plan(contexte):
    cfg, db = contexte
    res = mcp_server.enregistrer_plan(PAYLOAD_PLAN)

    assert res["enregistre"] is True
    assert res["seances"] == 2
    assert res["volume_km"] == 22.0
    # Le plan devient le plan courant, exploitable par le CLI
    assert db.get_meta("plan_courant")
    assert Plan.from_json(db.get_meta("plan_courant")).titre == "Semaine test"


def test_enregistrer_plan_puis_le_relire(contexte):
    mcp_server.enregistrer_plan(PAYLOAD_PLAN)
    res = mcp_server.plan_en_cours()

    assert res["titre"] == "Semaine test"
    assert len(res["seances"]) == 2
    assert res["points_de_vigilance"] == ["Attention au genou"]
    assert "adherence" in res


def test_enregistrer_plan_vide_refuse(contexte):
    with pytest.raises(ValueError, match="aucune séance"):
        mcp_server.enregistrer_plan({**PAYLOAD_PLAN, "seances": []})


def test_enregistrer_plan_mal_forme_refuse(contexte):
    with pytest.raises(ValueError):
        mcp_server.enregistrer_plan({"seances": "pas une liste"})


def test_bilan_sans_activite_message_actionnable(tmp_path, monkeypatch):
    cfg = Config(
        athlete=Athlete(prenom="Vide"),
        home=tmp_path,
        db_path=tmp_path / "vide.sqlite",
        reports_dir=tmp_path / "reports",
        token_dir=tmp_path / "tokens",
        config_path=tmp_path / "athlete.yml",
    )
    monkeypatch.setattr(mcp_server, "_config", cfg)
    monkeypatch.setattr(mcp_server, "_db", Database(cfg.db_path))

    with pytest.raises(ValueError, match="coach sync"):
        mcp_server.etat_de_forme()


def test_enregistrer_plan_applique_les_garde_fous(contexte):
    # Un plan venu de la conversation ne doit pas contourner `controler_plan`,
    # que `coach plan` applique déjà : ici, un « repos » qui se court.
    _, db = contexte
    douteux = {
        **PAYLOAD_PLAN,
        "seances": PAYLOAD_PLAN["seances"]
        + [
            {
                "date": (date.today() + timedelta(days=3)).isoformat(),
                "titre": "Repos couru",
                "type": "repos",
                "distance_km": 8.0,
                "duree_min": None,
                "objectif": "",
                "consignes": "",
                "etapes": [],
            }
        ],
    }
    res = mcp_server.enregistrer_plan(douteux)

    assert any("repos" in a for a in res["avertissements"])
    # Les avertissements sont persistés avec le plan courant
    assert Plan.from_json(db.get_meta("plan_courant")).avertissements


def test_enregistrer_plan_sain_sans_avertissement(contexte):
    res = mcp_server.enregistrer_plan(PAYLOAD_PLAN)
    assert res["avertissements"] == []
