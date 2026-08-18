"""Tests des calculs d'entraînement."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from runningwhale.analysis import (
    ACWR_MAX_SAIN,
    analyse_seance,
    bilan,
    charge_seance,
    etat_forme,
    format_duration,
    format_pace,
    projections,
    records,
    repartition_intensite,
    riegel,
    semaines,
    vma_estimee,
    zone_de_fc,
)
from runningwhale import analysis
from runningwhale.config import Athlete
from runningwhale.models import Activity, Lap


@pytest.fixture
def athlete() -> Athlete:
    return Athlete(
        prenom="Test",
        sexe="H",
        fc_max=190,
        fc_repos=50,
        allure_seuil_s_km=255,  # 4:15/km
        vma_kmh=17.0,
    )


def course(
    jours_avant: int = 0,
    km: float = 10.0,
    minutes: float = 50.0,
    fc: int | None = 150,
    laps: list[Lap] | None = None,
    activity_id: str | None = None,
) -> Activity:
    debut = datetime.now() - timedelta(days=jours_avant)
    return Activity(
        activity_id=activity_id or f"a{jours_avant}-{km}",
        debut=debut,
        type="running",
        nom="Sortie",
        distance_m=km * 1000,
        duree_s=minutes * 60,
        duree_mouvement_s=minutes * 60,
        fc_moy=fc,
        laps=laps or [],
    )


# ---- formatage ----

def test_format_pace():
    assert format_pace(270) == "4:30/km"
    assert format_pace(305.4) == "5:05/km"
    assert format_pace(None) == "—"
    assert format_pace(0) == "—"


def test_format_duration():
    assert format_duration(305) == "5:05"
    assert format_duration(3725) == "1h02:05"
    assert format_duration(None) == "—"


# ---- charge ----

def test_charge_utilise_trimp_quand_fc_disponible(athlete):
    # 60 min à 120 bpm = 50 % de réserve. Le TRIMP brut de Banister vaudrait
    # ~50, mais il est normalisé pour que 1 h au seuil (~85 % de réserve)
    # vaille 100, comme la voie par l'allure : ~50 × 100/167 ≈ 30.
    charge = charge_seance(course(minutes=60, fc=120), athlete)
    assert 25 < charge < 35


def test_charge_croit_avec_intensite(athlete):
    facile = charge_seance(course(minutes=60, fc=120), athlete)
    dur = charge_seance(course(minutes=60, fc=175), athlete)
    assert dur > facile * 2


def test_charge_bascule_sur_allure_sans_fc():
    sans_fc = Athlete(prenom="X", allure_seuil_s_km=255)
    # 10 km en 42:30 = exactement l'allure seuil → environ 100 points pour ~42 min
    charge = charge_seance(course(km=10, minutes=42.5, fc=None), sans_fc)
    assert 60 < charge < 90


def test_charge_repli_sur_duree_sans_aucune_donnee():
    nu = Athlete(prenom="X")
    assert charge_seance(course(minutes=60, fc=None), nu) == pytest.approx(60.0)


def test_charge_nulle_si_duree_nulle(athlete):
    vide = Activity(activity_id="v", debut=datetime.now(), type="running")
    assert charge_seance(vide, athlete) == 0.0


# ---- état de forme ----

def test_etat_forme_sans_activite(athlete):
    forme = etat_forme([], athlete)
    assert forme.ctl == 0 and forme.atl == 0 and forme.acwr is None


def test_charge_reguliere_donne_tsb_proche_de_zero(athlete):
    # Charge identique tous les jours pendant longtemps : CTL et ATL convergent
    # vers la même valeur, donc le TSB tend vers 0. Il faut dépasser largement
    # la fenêtre de 42 jours de la CTL, plus lente à converger que l'ATL.
    activites = [course(jours_avant=i, minutes=50) for i in range(180)]
    forme = etat_forme(activites, athlete)
    assert abs(forme.tsb) < 2
    assert forme.acwr == pytest.approx(1.0, abs=0.15)


def test_ctl_converge_plus_lentement_que_atl(athlete):
    # Sur 60 jours, l'ATL est déjà au plateau mais pas la CTL : le TSB est
    # encore négatif alors que la charge n'a jamais augmenté.
    # Avec le lissage de Banister (alpha = 1 - e^(-1/42)), la CTL n'est qu'à
    # 1 - e^(-60/42) ≈ 76 % de son plateau au jour 60 : le TSB attendu est
    # ≈ -24 % de la charge quotidienne (~ -13 ici). L'ancienne borne -10
    # encodait le lissage financier 2/(N+1), deux fois trop rapide.
    activites = [course(jours_avant=i, minutes=50) for i in range(60)]
    forme = etat_forme(activites, athlete)
    assert forme.atl > forme.ctl
    assert -20 < forme.tsb < -5


def test_montee_de_charge_brutale_leve_acwr(athlete):
    anciennes = [course(jours_avant=i, minutes=30) for i in range(8, 28)]
    recentes = [course(jours_avant=i, minutes=120, activity_id=f"r{i}") for i in range(7)]
    forme = etat_forme(anciennes + recentes, athlete)
    assert forme.acwr is not None and forme.acwr > ACWR_MAX_SAIN
    assert "risque" in forme.lecture_acwr or "vigilance" in forme.lecture_acwr


def test_affutage_donne_tsb_positif(athlete):
    # Grosse charge il y a un mois, puis quasi rien : la fraîcheur remonte
    anciennes = [course(jours_avant=i, minutes=90) for i in range(20, 60)]
    forme = etat_forme(anciennes, athlete)
    assert forme.tsb > 0


# ---- volumes ----

def test_semaines_agrege_par_lundi(athlete):
    activites = [course(jours_avant=i, km=10) for i in range(14)]
    resultat = semaines(activites, athlete, nombre=3)
    assert len(resultat) == 3
    assert sum(s.seances for s in resultat) >= 14 - 7  # au moins les 2 dernières semaines
    assert all(s.debut.weekday() == 0 for s in resultat)


def test_semaines_vides_restent_presentes(athlete):
    resultat = semaines([course(jours_avant=0)], athlete, nombre=4)
    assert len(resultat) == 4
    assert resultat[0].seances == 0
    assert resultat[-1].seances == 1


# ---- zones ----

def test_zone_de_fc_utilise_la_reserve(athlete):
    # réserve = 140 ; 50 + 0.65*140 = 141 → Z2
    assert zone_de_fc(141, athlete).startswith("Z2")
    assert zone_de_fc(180, athlete).startswith("Z5")
    assert zone_de_fc(None, athlete) is None


def test_zone_sans_fc_max_renvoie_none():
    assert zone_de_fc(150, Athlete(prenom="X")) is None


def test_repartition_privilegie_les_tours(athlete):
    laps = [
        Lap(index=1, distance_m=1000, duree_s=300, fc_moy=120, allure_s_km=300),
        Lap(index=2, distance_m=1000, duree_s=240, fc_moy=185, allure_s_km=240),
    ]
    r = repartition_intensite([course(laps=laps, fc=150)], athlete)
    assert r.base == "tours"
    assert r.facile_pct > 0 and r.dur_pct > 0


def test_repartition_sans_fc_est_vide():
    r = repartition_intensite([course(fc=None)], Athlete(prenom="X"))
    assert r.temps_total_s == 0
    assert "pas de données" in r.lecture


# ---- performances ----

def test_records_detecte_les_distances_de_reference():
    activites = [
        course(jours_avant=1, km=10.05, minutes=42, activity_id="dix"),
        course(jours_avant=2, km=10.0, minutes=45, activity_id="dix-lent"),
        course(jours_avant=3, km=7.0, minutes=35, activity_id="hors-format"),
    ]
    recs = records(activites)
    assert [r.libelle for r in recs] == ["10 km"]
    assert recs[0].temps_s < 43 * 60  # le plus rapide des deux 10 km


def test_records_normalise_la_distance():
    # 5,1 km en 21:00 doit être ramené à un 5 km légèrement plus rapide
    recs = records([course(km=5.1, minutes=21.0)])
    assert recs[0].temps_s < 21 * 60


def test_records_rejette_au_dela_de_la_tolerance():
    # 5,3 km, c'est 6 % de plus qu'un 5 km : hors tolérance
    assert records([course(km=5.3, minutes=22.0)]) == []


def test_riegel_extrapole_vers_le_haut():
    dix_km = 42 * 60
    semi = riegel(dix_km, 10, 21.0975)
    assert semi > dix_km * 2  # plus long donc plus lent au kilomètre
    assert semi < dix_km * 2.3


def test_projections_couvrent_toutes_les_distances():
    activites = [course(jours_avant=5, km=21.0975, minutes=95)]
    proj = projections(activites, Athlete(prenom="X"))
    assert {p.libelle for p in proj} == {"5 km", "10 km", "Semi-marathon", "Marathon"}
    marathon = next(p for p in proj if p.libelle == "Marathon")
    assert marathon.temps_s > 95 * 60 * 2


def test_projections_partent_de_la_meilleure_perf_pas_de_la_plus_longue():
    # Un 10 km de compétition rapide + une sortie longue tranquille de 21 km.
    # La projection doit s'appuyer sur le 10 km, sinon elle annoncerait sur
    # 10 km un temps plus lent que celui déjà réalisé.
    activites = [
        course(jours_avant=9, km=10.02, minutes=42.4, activity_id="course"),
        course(jours_avant=40, km=21.0975, minutes=110, activity_id="longue"),
    ]
    proj = projections(activites, Athlete(prenom="X"))
    dix = next(p for p in proj if p.libelle == "10 km")
    assert dix.temps_s <= 42.4 * 60 * 1.01
    assert "10 km" in proj[0].source


def test_meilleure_reference_compare_a_distance_egale():
    from runningwhale.analysis import meilleure_reference

    refs = records(
        [
            course(jours_avant=1, km=10.0, minutes=42, activity_id="rapide"),
            course(jours_avant=2, km=21.0975, minutes=110, activity_id="lente"),
        ]
    )
    assert meilleure_reference(refs).libelle == "10 km"


def test_meilleure_reference_sans_record():
    from runningwhale.analysis import meilleure_reference

    assert meilleure_reference([]) is None


def test_projections_vides_sans_reference():
    assert projections([course(km=7.0)], Athlete(prenom="X")) == []


# ---- VMA ----

def test_vma_profil_prioritaire(athlete):
    assert vma_estimee([], athlete) == 17.0


def test_vma_depuis_vo2max():
    # Centrale : 56/3,5 = 16,0 km/h. Servie en borne basse (−10 %) : une
    # estimation de montre ne se prescrit pas telle quelle (calculs v2).
    a = course()
    a.vo2max = 56.0
    assert vma_estimee([a], Athlete(prenom="X")) == pytest.approx(14.4, abs=0.1)


def test_vma_depuis_vo2max_provenance():
    a = course()
    a.vo2max = 56.0
    est = analysis.vma_avec_provenance([a], Athlete(prenom="X"))
    assert not est.mesuree
    assert est.valeur == est.fourchette[0] < est.fourchette[1]
    assert "VO2max" in est.source
    assert est.version == analysis.VERSION_CALCULS


def test_vma_depuis_5km():
    # 5 km en 20:00 = 15 km/h → VMA centrale ≈ 16,3 km/h, servie telle quelle :
    # à 92 % de VMA supposés, la centrale est déjà la borne basse.
    vma = vma_estimee([course(km=5.0, minutes=20.0)], Athlete(prenom="X"))
    assert vma == pytest.approx(16.3, abs=0.2)


def test_vma_depuis_5km_provenance():
    est = analysis.vma_avec_provenance(
        [course(km=5.0, minutes=20.0)], Athlete(prenom="X")
    )
    assert not est.mesuree
    assert est.valeur == est.fourchette[0] < est.fourchette[1]
    assert "Riegel" in est.source


def test_vma_declaree_est_mesuree():
    # Une VMA posée dans le profil est celle de l'athlète : aucune incertitude
    # ajoutée, et la provenance le dit.
    est = analysis.vma_avec_provenance([], Athlete(prenom="X", vma_kmh=17.0))
    assert est.mesuree
    assert est.valeur == 17.0
    assert est.fourchette == (17.0, 17.0)
    assert "profil" in est.source


# ---- analyse de séance ----

def test_decouplage_detecte_la_derive_cardiaque(athlete):
    # Même allure, FC qui grimpe → dérive positive
    laps = [
        Lap(index=1, distance_m=1000, duree_s=300, fc_moy=140, allure_s_km=300),
        Lap(index=2, distance_m=1000, duree_s=300, fc_moy=142, allure_s_km=300),
        Lap(index=3, distance_m=1000, duree_s=300, fc_moy=158, allure_s_km=300),
        Lap(index=4, distance_m=1000, duree_s=300, fc_moy=162, allure_s_km=300),
    ]
    analyse = analyse_seance(course(laps=laps), athlete)
    assert analyse.decouplage_pct is not None and analyse.decouplage_pct > 5
    assert "dérive" in analyse.lecture_decouplage


def test_decouplage_indisponible_sans_tours(athlete):
    analyse = analyse_seance(course(), athlete)
    assert analyse.decouplage_pct is None
    assert "non mesurable" in analyse.lecture_decouplage


def test_negative_split(athlete):
    laps = [
        Lap(index=1, distance_m=1000, duree_s=320, fc_moy=150, allure_s_km=320),
        Lap(index=2, distance_m=1000, duree_s=310, fc_moy=152, allure_s_km=310),
        Lap(index=3, distance_m=1000, duree_s=290, fc_moy=158, allure_s_km=290),
        Lap(index=4, distance_m=1000, duree_s=280, fc_moy=160, allure_s_km=280),
    ]
    assert analyse_seance(course(laps=laps), athlete).negative_split is True


def test_ecart_allure_seuil(athlete):
    # 10 km en 50 min = 5:00/km, seuil à 4:15 → 45 s/km plus lent
    analyse = analyse_seance(course(km=10, minutes=50), athlete)
    assert "45 s/km plus lent" in analyse.ecart_allure_seuil


# ---- bilan global ----

def test_bilan_assemble_tout(athlete):
    activites = [course(jours_avant=i, km=8 + i % 5) for i in range(30)]
    b = bilan(activites, athlete, nb_semaines=4)
    assert b.nb_activites == 30
    assert len(b.semaines) == 4
    assert b.vma_kmh == 17.0
    assert b.allures  # les allures dérivent de la VMA
    assert b.derniere_seance is not None


def test_bilan_ignore_les_activites_non_course(athlete):
    velo = course()
    velo.type = "cycling"
    b = bilan([velo], athlete)
    assert b.nb_activites == 0


# ---- régressions de l'audit du 2026-08-16 (calculs physiologiques) ----

def test_trimp_et_allure_sur_la_meme_echelle_au_seuil(athlete):
    # Les trois voies de charge alimentent la même CTL : une heure au seuil
    # doit valoir ~100 des deux côtés (convention TSS), pas ~167 pour le TRIMP
    # brut de Banister contre 100 pour la voie par l'allure.
    # FC seuil ~85 % de réserve : 50 + 0,85 × 140 = 169 bpm.
    par_fc = charge_seance(course(minutes=60, fc=169, km=14.1), athlete)
    sans_fc = Athlete(prenom="X", allure_seuil_s_km=255)
    par_allure = charge_seance(course(minutes=60, fc=None, km=3600 / 255), sans_fc)
    assert par_fc == pytest.approx(100.0, abs=5)
    assert par_allure == pytest.approx(100.0, abs=5)


def test_trimp_normalise_sur_la_fc_seuil_declaree():
    # Avec une LTHR déclarée, l'ancrage de la normalisation la suit.
    a = Athlete(prenom="X", sexe="H", fc_max=190, fc_repos=50, fc_seuil=169)
    assert charge_seance(course(minutes=60, fc=169), a) == pytest.approx(100.0, abs=1)


def test_charge_fc_aberrante_replie_sur_allure(athlete):
    # FC moyenne sous la FC de repos : donnée absurde → la voie par l'allure
    # prend le relais au lieu de rendre une charge nulle en silence.
    assert charge_seance(course(minutes=50, km=10, fc=45), athlete) > 30


def test_lissage_suit_la_constante_de_temps_de_42_jours(athlete):
    # Après exactement 42 jours de charge constante, une EMA de constante de
    # temps 42 j est à 1 − 1/e ≈ 63,2 % de son plateau : c'est la définition
    # de la constante de temps (Banister / TrainingPeaks). L'ancien lissage
    # financier 2/(N+1) donnait ~87 % au jour 42 (constante effective ~21 j).
    activites = [course(jours_avant=i, minutes=50) for i in range(42)]
    forme = etat_forme(activites, athlete)
    plateau = charge_seance(course(minutes=50), athlete)
    assert forme.ctl / plateau == pytest.approx(1 - math.exp(-1), abs=0.02)


def test_acwr_absent_sans_profondeur_d_historique(athlete):
    # Une seule séance d'historique : l'ACWR couplé vaudrait mécaniquement 4,0
    # et annoncerait un « risque de blessure élevé » dès la première sortie.
    forme = etat_forme([course(jours_avant=1, minutes=60)], athlete)
    assert forme.acwr is None
    assert "pas assez d'historique" in forme.lecture_acwr


def test_zone_fc_sous_la_fc_repos_n_est_pas_z5(athlete):
    # Régression : un ratio de Karvonen négatif ne matchait aucune zone et
    # retombait sur Z5 via le repli final.
    assert zone_de_fc(45, athlete).startswith("Z1")


def test_decouplage_coupe_a_la_moitie_du_temps(athlete):
    # Échauffement de 30 min + 6 × 400 m rapides : la coupe à mi-nombre-de-
    # tours mettait 2 fractions rapides dans la « première moitié » (−11,6 %) ;
    # la coupe temporelle compare échauffement vs fractionné (−13,1 %).
    laps = [Lap(index=1, distance_m=6000, duree_s=1800, fc_moy=140, allure_s_km=300)]
    laps += [
        Lap(index=i, distance_m=400, duree_s=90, fc_moy=165, allure_s_km=225)
        for i in range(2, 8)
    ]
    analyse = analyse_seance(course(laps=laps), athlete)
    assert analyse.decouplage_pct == pytest.approx(-13.1, abs=0.3)


def test_projection_lointaine_est_signalee():
    # Depuis un 5 km, le marathon (×8,4) et le semi (×4,2) sortent du domaine
    # de validité de Riegel ; le 10 km (×2) reste raisonnable.
    proj = projections([course(km=5.0, minutes=20.0)], Athlete(prenom="X"))
    par_libelle = {p.libelle: p for p in proj}
    assert par_libelle["Marathon"].extrapolation_lointaine is True
    assert par_libelle["Semi-marathon"].extrapolation_lointaine is True
    assert par_libelle["10 km"].extrapolation_lointaine is False
    assert par_libelle["5 km"].extrapolation_lointaine is False


# ---- régressions trouvées à l'audit du 2026-08-16 ----

@pytest.mark.parametrize(
    "type_key",
    [
        "running", "trail_running", "treadmill_running", "track_running",
        "street_running", "indoor_running", "ultra_running", "virtual_running",
    ],
)
def test_tous_les_types_de_course_garmin_sont_reconnus(type_key):
    a = course()
    a.type = type_key
    assert a.est_course is True


@pytest.mark.parametrize(
    "type_key", ["cycling", "hiking", "walking", "open_water_swimming", "multi_sport"]
)
def test_les_autres_sports_ne_sont_pas_des_courses(type_key):
    a = course()
    a.type = type_key
    assert a.est_course is False
