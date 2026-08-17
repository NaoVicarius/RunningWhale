"""Tests de la composition corporelle et des mesures immuables du profil."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from runningwhale.composition import Composition, tendance
from runningwhale.config import Athlete, _parse_date, _taille_en_cm
from runningwhale.db import Database


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "test.db")


# ---- mesures dérivées ----

def test_masse_grasse_et_maigre_se_deduisent_du_pourcentage():
    m = Composition(jour=date(2026, 8, 16), poids_kg=78.4, graisse_pct=21.2)
    assert m.masse_grasse_kg == pytest.approx(16.6, abs=0.05)
    assert m.masse_maigre_kg == pytest.approx(61.78, abs=0.05)


def test_masse_maigre_mesuree_prime_sur_la_masse_maigre_calculee():
    # La balance donne les deux : on garde ce qu'elle a mesuré plutôt que de
    # recalculer depuis un pourcentage déjà arrondi.
    m = Composition(
        jour=date(2026, 8, 16), poids_kg=78.4, graisse_pct=21.2,
        masse_hors_graisse_kg=61.7,
    )
    assert m.masse_maigre_kg == 61.7


def test_sans_pourcentage_de_graisse_les_masses_restent_inconnues():
    m = Composition(jour=date(2026, 8, 16), poids_kg=78.4)
    assert m.masse_grasse_kg is None
    assert m.masse_maigre_kg is None


# ---- tendance ----

def _pesees(*valeurs: tuple[int, float]) -> list[Composition]:
    base = date(2026, 8, 16)
    return [
        Composition(jour=base - timedelta(days=recul), poids_kg=kg)
        for recul, kg in valeurs
    ]


def test_tendance_compare_deux_moyennes_et_non_deux_pesees():
    # Une pesée isolée bouge de plus d'un kilo d'un matin à l'autre : la
    # tendance doit lisser, sinon elle raconte l'hydratation, pas la forme.
    mesures = _pesees(
        (0, 76.6), (3, 77.1), (7, 77.6),      # deux dernières semaines
        (16, 79.1), (20, 79.6), (25, 79.1),   # les deux d'avant
    )
    t = tendance(mesures, fenetre_jours=14)
    assert t is not None
    assert t.recent_kg == pytest.approx(77.1, abs=0.01)
    assert t.precedent_kg == pytest.approx(79.27, abs=0.01)
    assert t.delta_kg == pytest.approx(-2.17, abs=0.01)
    assert "baisse" in t.lecture


def test_tendance_dit_stable_sous_le_seuil_de_bruit():
    t = tendance(_pesees((0, 77.6), (16, 77.7)), fenetre_jours=14)
    assert t is not None and t.lecture == "poids stable"


def test_des_pesees_serrees_donnent_quand_meme_une_tendance():
    # Cas réel : cinq pesées assidues sur treize jours. Elles tiennent toutes
    # dans la fenêtre récente, donc la comparaison à la fenêtre précédente n'a
    # rien à se mettre sous la dent — alors que la direction est nette. La
    # série est alors coupée en deux moitiés.
    mesures = _pesees(
        (13, 80.3), (12, 79.5), (11, 78.9), (8, 78.7), (0, 78.4)
    )
    t = tendance(mesures, fenetre_jours=14)
    assert t is not None
    assert t.delta_kg is not None and t.delta_kg < 0
    assert "baisse" in t.lecture
    assert t.span_jours == 13


def test_le_repli_exige_assez_de_pesees_et_de_jours():
    # Trois pesées sur deux jours ne racontent que l'hydratation.
    t = tendance(_pesees((2, 79.1), (1, 106.0), (0, 78.1)))
    assert t is not None and t.delta_kg is None


def test_la_fenetre_glissante_prime_sur_le_repli():
    # Dès qu'il y a de quoi comparer deux vraies fenêtres, on ne coupe pas en
    # deux : la fenêtre fixe est la lecture la plus stable.
    mesures = _pesees((0, 76.6), (7, 77.6), (16, 79.1), (25, 79.6))
    t = tendance(mesures, fenetre_jours=14)
    assert t is not None
    assert t.recent_kg == pytest.approx(77.1)
    assert t.precedent_kg == pytest.approx(79.35)


def test_une_seule_pesee_ne_produit_pas_de_tendance():
    t = tendance(_pesees((0, 78.4)))
    assert t is not None
    assert t.delta_kg is None
    assert "pas encore assez" in t.lecture


def test_aucune_pesee_ne_produit_rien():
    assert tendance([]) is None


# ---- persistance ----

def test_une_seule_pesee_par_jour(db):
    jour = date(2026, 8, 16)
    assert db.upsert_composition(Composition(jour=jour, poids_kg=78.4)) is True
    assert db.upsert_composition(Composition(jour=jour, poids_kg=78.0)) is False

    mesures = db.compositions()
    assert len(mesures) == 1
    assert mesures[0].poids_kg == 78.0  # la dernière remplace la précédente


def test_les_pesees_reviennent_dans_l_ordre_chronologique(db):
    for recul in (0, 10, 5):
        db.upsert_composition(
            Composition(jour=date(2026, 8, 16) - timedelta(days=recul), poids_kg=100 + recul)
        )
    jours = [m.jour for m in db.compositions()]
    assert jours == sorted(jours)


def test_la_derniere_pesee_est_la_plus_recente(db):
    db.upsert_composition(Composition(jour=date(2026, 8, 1), poids_kg=79.6))
    db.upsert_composition(Composition(jour=date(2026, 8, 16), poids_kg=78.4))
    derniere = db.derniere_composition()
    assert derniere is not None and derniere.poids_kg == 78.4


def test_tous_les_champs_de_la_balance_survivent_a_l_aller_retour(db):
    mesure = Composition(
        jour=date(2026, 8, 16), poids_kg=78.4, imc=24.8, graisse_pct=21.2,
        muscle_squelettique_pct=50.8, masse_hors_graisse_kg=61.7,
        gras_sous_cutane_pct=18.3, graisse_viscerale=10, eau_pct=56.9,
        masse_musculaire_kg=58.6, masse_osseuse_kg=3.1, proteines_pct=18.0,
        metabolisme_base_kcal=1780, age_metabolique=32, source="balance Renpho",
    )
    db.upsert_composition(mesure)
    relue = db.derniere_composition()
    assert relue == mesure


# ---- mesures immuables du profil ----

def test_l_age_se_calcule_depuis_la_naissance():
    # Un âge écrit en dur se périme en silence ; la date de naissance, non.
    athlete = Athlete(naissance=date(1990, 5, 15))
    attendu = date.today().year - 1990 - (
        0 if (date.today().month, date.today().day) >= (5, 15) else 1
    )
    assert athlete.age == attendu


def test_l_anniversaire_pas_encore_passe_retire_un_an():
    demain = date.today() + timedelta(days=1)
    athlete = Athlete(naissance=date(1990, demain.month, demain.day))
    assert athlete.age == date.today().year - 1990 - 1


def test_la_naissance_prime_sur_l_age_declare():
    athlete = Athlete(naissance=date(1990, 5, 1), age_declare=34)
    assert athlete.age != 34


def test_sans_naissance_l_age_declare_sert_encore():
    assert Athlete(age_declare=34).age == 34
    assert Athlete().age is None


def test_imc_calcule_depuis_taille_et_poids():
    athlete = Athlete(taille_cm=178, poids_kg=78.4)
    assert athlete.imc == pytest.approx(24.74, abs=0.05)


def test_imc_inconnu_sans_taille():
    assert Athlete(poids_kg=78.4).imc is None
    assert Athlete(taille_cm=0, poids_kg=78.4).imc is None


@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("1m78", 178.0),
        (1.78, 178.0),     # mètres — le réflexe le plus courant
        (178, 178.0),
        ("178cm", 178.0),
        ("1,78", 178.0),
        ("", None),
        (None, None),
        ("grand", None),
    ],
)
def test_taille_acceptee_dans_ses_ecritures_courantes(brut, attendu):
    assert _taille_en_cm(brut) == attendu


@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("1990-05-15", date(1990, 5, 15)),
        ("1990-05", date(1990, 5, 1)),   # mois seul : premier du mois
        (date(1990, 5, 15), date(1990, 5, 15)),
        ("", None),
        ("mai 1990", None),
    ],
)
def test_naissance_acceptee_dans_ses_ecritures_courantes(brut, attendu):
    assert _parse_date(brut) == attendu


# ---- allures sous la transition marche/course ----

def test_transition_marche_course_depend_de_la_taille():
    from runningwhale.analysis import vitesse_transition_marche_course

    grand = vitesse_transition_marche_course(178)
    petit = vitesse_transition_marche_course(160)
    # Des jambes plus longues marchent vite plus longtemps
    assert grand > petit
    assert 7.5 < grand < 9.0


def test_allures_faciles_immarchables_sont_signalees():
    # Cas réel : VMA 9,1 km/h chez un athlète d'1m78. 65 % de VMA valent
    # 5,9 km/h — plus lent que sa marche. Servir cette allure sans rien dire
    # ferait prescrire une séance impossible à exécuter en courant.
    from runningwhale.analysis import allures_entrainement, allures_sous_la_marche

    athlete = Athlete(taille_cm=178, fc_max=191, fc_repos=66)
    allures = allures_entrainement(9.1, athlete)
    assert "⚠️" in allures["Endurance fondamentale (65-70 % VMA)"]

    alerte = allures_sous_la_marche(9.1, athlete)
    assert alerte is not None
    assert "transition marche/course" in alerte
    # L'alerte doit dire quoi faire à la place, pas seulement constater
    assert "153" in alerte  # plafond de FC calculé sur sa réserve (66 + 0,7 × 125)


def test_pas_d_alerte_pour_un_coureur_entraine():
    from runningwhale.analysis import allures_entrainement, allures_sous_la_marche

    athlete = Athlete(taille_cm=178)
    assert allures_sous_la_marche(17.0, athlete) is None
    assert "⚠️" not in allures_entrainement(17.0, athlete)[
        "Endurance fondamentale (65-70 % VMA)"
    ]


def test_alerte_reste_utile_sans_reperes_cardiaques():
    from runningwhale.analysis import allures_sous_la_marche

    alerte = allures_sous_la_marche(9.1, Athlete(taille_cm=178))
    assert alerte is not None and "sensation" in alerte
