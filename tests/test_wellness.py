"""Tests de l'extraction et de l'analyse des données de récupération.

Les réponses Garmin utilisées ici reproduisent les formes observées en pratique.
Comme leur structure n'est pas documentée publiquement et varie selon la montre,
l'extraction doit rester tolérante : un champ absent vaut None, jamais une erreur.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from runningwhale.analysis import (
    CIBLE_SOMMEIL_H,
    HAUSSE_FC_REPOS_ALERTE,
    recuperation,
)
from runningwhale.wellness import Wellness, chercher, depuis_garmin

SOMMEIL = {
    "dailySleepDTO": {
        "calendarDate": "2026-08-14",
        "sleepTimeSeconds": 27000,  # 7 h 30
        "deepSleepSeconds": 5400,
        "remSleepSeconds": 6300,
        "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}},
    },
    "restingHeartRate": 48,
}

VFC = {
    "hrvSummary": {
        "calendarDate": "2026-08-14",
        "lastNightAvg": 61,
        "status": "BALANCED",
        "baseline": {"balancedLow": 52, "balancedUpper": 74},
    }
}

READINESS = [{"score": 74, "level": "HIGH", "recoveryTime": 900, "sleepScore": 80}]

BODY_BATTERY = [{"bodyBatteryValuesArray": [[0, 22], [1, 88], [2, 45]]}]

STATS = {"restingHeartRate": 48, "averageStressLevel": 27}


# ---- chercher() ----

def test_chercher_suit_un_chemin_pointe():
    assert chercher(SOMMEIL, "dailySleepDTO.sleepTimeSeconds") == 27000


def test_chercher_essaie_les_chemins_dans_l_ordre():
    plat = {"sleepTimeSeconds": 100}
    assert chercher(plat, "dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds") == 100


def test_chercher_renvoie_none_si_aucun_chemin_ne_marche():
    assert chercher(SOMMEIL, "inexistant.champ") is None


def test_chercher_traverse_les_listes():
    assert chercher({"a": [{"b": 7}]}, "a.0.b") == 7


def test_chercher_ne_leve_pas_sur_type_inattendu():
    assert chercher({"a": "texte"}, "a.b.c") is None


# ---- extraction ----

def test_extraction_complete():
    w = depuis_garmin(date(2026, 8, 14), SOMMEIL, VFC, READINESS, BODY_BATTERY, STATS)
    assert w.sommeil_h == 7.5
    assert w.score_sommeil == 82
    assert w.vfc_ms == 61
    assert w.vfc_statut == "équilibrée"
    assert w.fc_repos == 48
    assert w.readiness_score == 74
    assert w.readiness_niveau == "élevée"
    assert w.body_battery_min == 22 and w.body_battery_max == 88
    assert w.stress_moyen == 27
    assert not w.est_vide


def test_extraction_partielle_ne_leve_pas():
    # Une montre sans capteur de VFC ni readiness
    w = depuis_garmin(date(2026, 8, 14), sommeil=SOMMEIL)
    assert w.sommeil_h == 7.5
    assert w.vfc_ms is None
    assert w.readiness_score is None
    assert not w.est_vide


def test_journee_sans_aucune_donnee_est_vide():
    assert depuis_garmin(date(2026, 8, 14)).est_vide


def test_extraction_tolere_des_formes_inattendues():
    w = depuis_garmin(
        date(2026, 8, 14),
        sommeil={"dailySleepDTO": {"sleepTimeSeconds": "pas un nombre"}},
        vfc={"totalement": "autre chose"},
        readiness=[],
    )
    assert w.sommeil_s is None
    assert w.vfc_ms is None


def test_raw_est_conserve():
    w = depuis_garmin(date(2026, 8, 14), SOMMEIL, VFC)
    assert w.raw["sommeil"] == SOMMEIL
    assert w.raw["vfc"] == VFC


def test_temps_recup_converti_en_heures():
    # Garmin renvoie des minutes ; 1560 min = 26 h
    w = depuis_garmin(date(2026, 8, 14), readiness=[{"score": 40, "recoveryTime": 1560}])
    assert w.temps_recup_h == 26.0


def test_vfc_sous_baseline():
    w = depuis_garmin(
        date(2026, 8, 14),
        vfc={"hrvSummary": {"lastNightAvg": 44, "baseline": {"balancedLow": 52}}},
    )
    assert w.vfc_sous_baseline is True


def test_vfc_sous_baseline_indeterminee_sans_baseline():
    assert depuis_garmin(date(2026, 8, 14), vfc={"hrvSummary": {"lastNightAvg": 44}}).vfc_sous_baseline is None


# ---- analyse ----

def jour(i: int, **kwargs) -> Wellness:
    """Journée de récupération à i jours dans le passé, valeurs normales par défaut."""
    defauts = dict(
        sommeil_s=8 * 3600,
        vfc_ms=62.0,
        vfc_baseline_bas=52.0,
        vfc_baseline_haut=74.0,
        fc_repos=48,
        readiness_score=75,
    )
    defauts.update(kwargs)
    return Wellness(jour=date.today() - timedelta(days=i), **defauts)


def test_recuperation_sans_donnees():
    r = recuperation([])
    assert not r.disponible
    assert r.alertes == []
    assert "aucune donnée" in r.lecture


def test_recuperation_normale_ne_leve_aucune_alerte():
    r = recuperation([jour(i) for i in range(14)])
    assert r.disponible
    assert r.sommeil_moyen_h == 8.0
    assert r.dette_sommeil_h == 0.0
    assert r.alertes == []
    assert r.lecture == "récupération dans la norme"


def test_alerte_vfc_sous_baseline_repetee():
    jours = [jour(0, vfc_ms=44.0), jour(1, vfc_ms=45.0)] + [jour(i) for i in range(2, 10)]
    r = recuperation(jours)
    assert r.vfc_jours_sous_baseline == 2
    assert any("VFC sous la fourchette" in a for a in r.alertes)


def test_une_seule_nuit_de_vfc_basse_ne_declenche_pas():
    jours = [jour(0, vfc_ms=44.0)] + [jour(i) for i in range(1, 10)]
    r = recuperation(jours)
    assert r.vfc_jours_sous_baseline == 1
    assert not any("VFC" in a for a in r.alertes)


def test_alerte_fc_repos_en_hausse():
    recents = [jour(i, fc_repos=53) for i in range(7)]
    anciens = [jour(i, fc_repos=47) for i in range(7, 28)]
    r = recuperation(recents + anciens)
    assert r.derive_fc_repos is not None
    assert r.derive_fc_repos >= HAUSSE_FC_REPOS_ALERTE
    assert any("FC de repos en hausse" in a for a in r.alertes)


def test_alerte_dette_de_sommeil():
    jours = [jour(i, sommeil_s=6 * 3600) for i in range(7)]
    r = recuperation(jours)
    # 2 h de manque par nuit sur 7 nuits = 14 h de dette
    assert r.dette_sommeil_h == pytest.approx(14.0)
    assert any("dette de sommeil" in a for a in r.alertes)


def test_alerte_readiness_basse():
    r = recuperation([jour(0, readiness_score=31)] + [jour(i) for i in range(1, 7)])
    assert any("readiness" in a.lower() for a in r.alertes)


def test_journees_vides_ignorees():
    r = recuperation([Wellness(jour=date.today()), jour(1)])
    assert r.jours_couverts == 1


def test_cible_sommeil_utilisee_pour_la_dette():
    r = recuperation([jour(0, sommeil_s=(CIBLE_SOMMEIL_H - 1) * 3600)])
    assert r.dette_sommeil_h == pytest.approx(1.0)


# ---- régressions trouvées à l'audit du 2026-08-16 ----

@pytest.mark.parametrize(
    "lignes",
    [
        [[1755000000000, 42], [1755003600000, 88]],
        # Forme fréquente : le niveau est en troisième position, pas en seconde
        [[1755000000000, "MEASURED", 42], [1755003600000, "MEASURED", 88]],
        [[1755000000000, "MEASURED", 42], [1755003600000, 88]],
    ],
)
def test_body_battery_supporte_les_deux_formes(lignes):
    w = depuis_garmin(date(2026, 8, 14), body_battery=[{"bodyBatteryValuesArray": lignes}])
    assert (w.body_battery_min, w.body_battery_max) == (42, 88)


def test_body_battery_ignore_l_horodatage():
    # L'horodatage en millisecondes ne doit jamais être pris pour un niveau
    w = depuis_garmin(
        date(2026, 8, 14),
        body_battery=[{"bodyBatteryValuesArray": [[1755000000000, 55]]}],
    )
    assert w.body_battery_max == 55


def test_body_battery_ligne_malformee():
    w = depuis_garmin(
        date(2026, 8, 14),
        body_battery=[{"bodyBatteryValuesArray": [["rien", "d'utile"], None, 42]}],
    )
    assert w.body_battery_max is None


@pytest.mark.parametrize(
    "minutes,heures", [(30, 0.5), (90, 1.5), (600, 10.0), (1560, 26.0)]
)
def test_temps_recuperation_est_monotone(minutes, heures):
    # L'ancienne heuristique convertissait au-delà de 72 seulement, ce qui
    # donnait 40 → 40 h mais 90 → 1,5 h : plus de récupération, chiffre plus petit.
    w = depuis_garmin(date(2026, 8, 14), readiness=[{"score": 50, "recoveryTime": minutes}])
    assert w.temps_recup_h == heures


def test_temps_recuperation_croit_avec_la_valeur_brute():
    valeurs = [
        depuis_garmin(date(2026, 8, 14), readiness=[{"score": 50, "recoveryTime": m}]).temps_recup_h
        for m in (10, 50, 100, 500, 2000)
    ]
    assert valeurs == sorted(valeurs)
