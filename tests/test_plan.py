"""Tests du plan structuré : parsing, ICS, conversion Garmin, adhérence."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from runningwhale.models import Activity
from runningwhale.plan import (
    Etape,
    Plan,
    Seance,
    allure_vers_ms,
    rapprocher,
    seance_vers_workout,
    to_ics,
)

PAYLOAD = {
    "titre": "Semaine de reprise",
    "focus": "Reconstruire l'endurance fondamentale",
    "resume": "Charge volontairement basse après trois semaines chargées.",
    "volume_total_km": 42.0,
    "points_de_vigilance": ["Surveiller le tendon d'Achille droit"],
    "seances": [
        {
            "date": "2026-08-17",
            "titre": "Footing d'endurance",
            "type": "endurance",
            "distance_km": 10.0,
            "duree_min": 55.0,
            "objectif": "Volume aérobie",
            "consignes": "Reste en conversation permanente.",
            "etapes": [
                {
                    "role": "effort",
                    "repetitions": 1,
                    "duree_min": 55.0,
                    "distance_km": 10.0,
                    "allure": "5:30/km",
                    "libelle": "Footing continu",
                }
            ],
        },
        {
            "date": "2026-08-19",
            "titre": "Séance de seuil",
            "type": "seuil",
            "distance_km": 12.0,
            "duree_min": 65.0,
            "objectif": "Développer le seuil",
            "consignes": "Allure tenue, pas de sur-régime.",
            "etapes": [
                {
                    "role": "echauffement",
                    "repetitions": 1,
                    "duree_min": 20.0,
                    "distance_km": None,
                    "allure": "5:30/km",
                    "libelle": "Échauffement progressif",
                },
                {
                    "role": "effort",
                    "repetitions": 4,
                    "duree_min": 6.0,
                    "distance_km": None,
                    "allure": "4:15/km",
                    "libelle": "Bloc au seuil",
                },
                {
                    "role": "recuperation",
                    "repetitions": 4,
                    "duree_min": 2.0,
                    "distance_km": None,
                    "allure": None,
                    "libelle": "Trot de récupération",
                },
                {
                    "role": "retour_au_calme",
                    "repetitions": 1,
                    "duree_min": 10.0,
                    "distance_km": None,
                    "allure": None,
                    "libelle": "Retour au calme",
                },
            ],
        },
        {
            "date": "2026-08-21",
            "titre": "Repos",
            "type": "repos",
            "distance_km": None,
            "duree_min": None,
            "objectif": "Récupération",
            "consignes": "Vraie journée off.",
            "etapes": [],
        },
    ],
}


@pytest.fixture
def plan() -> Plan:
    return Plan.from_payload(PAYLOAD)


# ---- parsing ----

def test_parsing_du_payload(plan):
    assert plan.titre == "Semaine de reprise"
    assert len(plan.seances) == 3
    assert plan.debut == date(2026, 8, 17)
    assert plan.fin == date(2026, 8, 21)
    assert plan.seances[1].etapes[1].repetitions == 4


def test_seances_triees_par_date():
    melange = dict(PAYLOAD)
    melange["seances"] = list(reversed(PAYLOAD["seances"]))
    p = Plan.from_payload(melange)
    assert [s.date for s in p.seances] == sorted(s.date for s in p.seances)


def test_aller_retour_json(plan):
    reconstruit = Plan.from_json(plan.to_json())
    assert reconstruit.titre == plan.titre
    assert [s.date for s in reconstruit.seances] == [s.date for s in plan.seances]
    assert reconstruit.seances[1].etapes[1].allure == "4:15/km"


def test_date_invalide_retombe_sur_aujourdhui():
    payload = {"titre": "x", "focus": "", "resume": "", "seances": [
        {"date": "pas-une-date", "titre": "t", "type": "endurance",
         "distance_km": None, "duree_min": None, "objectif": "", "consignes": "",
         "etapes": []}
    ]}
    assert Plan.from_payload(payload).seances[0].date == date.today()


def test_volume_calcule(plan):
    assert plan.volume_calcule_km == 22.0  # 10 + 12, le repos ne compte pas


def test_duree_estimee_depuis_les_etapes():
    seance = Seance(
        date=date.today(), titre="Fractionné", type="vma",
        etapes=[
            Etape(role="echauffement", libelle="Éch.", duree_min=15),
            Etape(role="effort", libelle="30/30", duree_min=1, repetitions=10),
        ],
    )
    assert seance.duree_estimee_s == pytest.approx((15 + 10) * 60)


# ---- allures ----

@pytest.mark.parametrize(
    "texte,attendu",
    [
        ("4:30/km", 1000 / 270),
        ("5:00", 1000 / 300),
        ("environ 4:15 au kilomètre", 1000 / 255),
        ("4'15", 1000 / 255),
    ],
)
def test_allure_vers_ms(texte, attendu):
    assert allure_vers_ms(texte) == pytest.approx(attendu, rel=1e-3)


def test_allure_illisible_renvoie_none():
    assert allure_vers_ms(None) is None
    assert allure_vers_ms("allure libre") is None


# ---- ICS ----

def test_ics_structure(plan):
    ics = to_ics(plan)
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.rstrip().endswith("END:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == 2  # le repos n'est pas un événement
    assert "\r\n" in ics  # la RFC 5545 impose CRLF


def test_ics_echappe_les_caracteres_speciaux():
    p = Plan.from_payload(
        {**PAYLOAD, "titre": "Semaine; test, spéciale"}
    )
    ics = to_ics(p)
    assert "X-WR-CALNAME:Semaine\\; test\\, spéciale" in ics


def test_ics_replie_les_longues_lignes():
    payload = {**PAYLOAD}
    payload["seances"] = [
        {**PAYLOAD["seances"][0], "titre": "T" * 200}
    ]
    for ligne in to_ics(Plan.from_payload(payload)).split("\r\n"):
        assert len(ligne.encode("utf-8")) <= 75


# ---- conversion Garmin ----

def test_conversion_en_workout_garmin(plan):
    pytest.importorskip("pydantic")
    seance = plan.seances[1]  # la séance de seuil
    workout = seance_vers_workout(seance)
    data = workout.to_dict()

    assert data["workoutName"] == "Séance de seuil"
    steps = data["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 4
    # Les blocs répétés deviennent des groupes de répétition
    groupes = [s for s in steps if s.get("stepType", {}).get("stepTypeKey") == "repeat"]
    assert len(groupes) == 2
    assert groupes[0]["numberOfIterations"] == 4


def test_cible_allure_transmise(plan):
    pytest.importorskip("pydantic")
    steps = seance_vers_workout(plan.seances[0]).to_dict()["workoutSegments"][0]["workoutSteps"]
    step = steps[0]
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    # 5:30/km = 3,03 m/s, avec une fourchette de ±5 %
    assert step["targetValueOne"] < 3.03 < step["targetValueTwo"]


def test_seance_sans_etape_produit_un_bloc_unique():
    pytest.importorskip("pydantic")
    seance = Seance(date=date.today(), titre="Footing", type="endurance", duree_min=45)
    steps = seance_vers_workout(seance).to_dict()["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 1
    assert steps[0]["endConditionValue"] == pytest.approx(45 * 60)


# ---- adhérence ----

def _course(jour: date, km: float, aid: str) -> Activity:
    return Activity(
        activity_id=aid,
        debut=datetime.combine(jour, datetime.min.time().replace(hour=18)),
        type="running",
        distance_m=km * 1000,
        duree_s=km * 300,
        duree_mouvement_s=km * 300,
    )


def test_adherence_apparie_les_seances(plan):
    activites = [
        _course(date(2026, 8, 17), 10.2, "a1"),
        _course(date(2026, 8, 19), 11.8, "a2"),
    ]
    a = rapprocher(plan, activites)
    assert a.prevues == 2 and a.realisees == 2
    assert a.taux == 100.0
    assert plan.seances[0].activity_id == "a1"


def test_adherence_signale_les_manquees(plan):
    a = rapprocher(plan, [_course(date(2026, 8, 17), 10.0, "a1")])
    assert a.realisees == 1
    assert a.taux == 50.0


def test_adherence_ne_reutilise_pas_une_activite(plan):
    # Une seule course, à cheval sur les deux dates prévues (tolérance 1 jour)
    a = rapprocher(plan, [_course(date(2026, 8, 18), 10.0, "unique")])
    assert a.realisees == 1


def test_adherence_repere_les_sorties_hors_plan(plan):
    activites = [
        _course(date(2026, 8, 17), 10.0, "a1"),
        _course(date(2026, 8, 19), 12.0, "a2"),
        _course(date(2026, 8, 20), 6.0, "bonus"),
    ]
    a = rapprocher(plan, activites)
    assert [x.activity_id for x in a.hors_plan] == ["bonus"]
    assert a.km_realises == 28.0


def test_adherence_ignore_les_jours_de_repos(plan):
    a = rapprocher(plan, [])
    assert a.prevues == 2  # la journée de repos n'est pas comptée


# ---- contrôles de vraisemblance (controler_plan) ----

def _seance_simple(jour: date, **surcharge) -> Seance:
    base = dict(date=jour, titre="Footing", type="endurance", distance_km=8.0)
    base.update(surcharge)
    return Seance(**base)


def test_controle_plan_sain_sans_avertissement():
    debut = date.today() + timedelta(days=1)
    p = Plan(
        titre="S", focus="f", resume="r",
        seances=[
            _seance_simple(debut),
            _seance_simple(debut + timedelta(days=2)),
            Seance(date=debut + timedelta(days=3), titre="Repos", type="repos"),
        ],
    )
    from runningwhale.plan import controler_plan
    assert controler_plan(p, debut=debut, vma_kmh=15.0, volume_recent_km=20.0) == []


def test_controle_date_hors_semaine():
    from runningwhale.plan import controler_plan
    debut = date.today() + timedelta(days=1)
    p = Plan(titre="S", focus="f", resume="r",
             seances=[_seance_simple(debut + timedelta(days=12))])
    avert = controler_plan(p, debut=debut)
    assert len(avert) == 1
    assert "hors de la semaine" in avert[0]


def test_controle_date_passee():
    from runningwhale.plan import controler_plan
    debut = date.today() - timedelta(days=3)
    p = Plan(titre="S", focus="f", resume="r",
             seances=[_seance_simple(debut + timedelta(days=1))])
    avert = controler_plan(p, debut=debut)
    assert len(avert) == 1
    assert "déjà passée" in avert[0]


def test_controle_repos_avec_distance():
    from runningwhale.plan import controler_plan
    demain = date.today() + timedelta(days=1)
    p = Plan(titre="S", focus="f", resume="r",
             seances=[Seance(date=demain, titre="Repos", type="repos", distance_km=6.0)])
    avert = controler_plan(p, debut=demain)
    assert any("repos" in a for a in avert)


def test_controle_allure_plus_rapide_que_la_vma():
    from runningwhale.plan import controler_plan
    demain = date.today() + timedelta(days=1)
    seance = _seance_simple(
        demain,
        etapes=[Etape(role="effort", libelle="400 m", allure="3:00/km")],
    )
    # VMA 15 km/h = 4:00/km : demander du 3:00/km est intenable.
    avert = controler_plan(Plan(titre="S", focus="f", resume="r", seances=[seance]),
                           debut=demain, vma_kmh=15.0)
    assert any("VMA" in a for a in avert)
    # La même allure avec une VMA de 21 km/h ne déclenche rien.
    assert controler_plan(Plan(titre="S", focus="f", resume="r", seances=[seance]),
                          debut=demain, vma_kmh=21.0) == []


def test_controle_volume_aberrant():
    from runningwhale.plan import controler_plan
    debut = date.today() + timedelta(days=1)
    p = Plan(titre="S", focus="f", resume="r",
             seances=[_seance_simple(debut + timedelta(days=i), distance_km=20.0)
                      for i in range(4)])
    avert = controler_plan(p, debut=debut, volume_recent_km=30.0)
    assert any("volume" in a for a in avert)
    assert controler_plan(p, debut=debut, volume_recent_km=70.0) == []


def test_avertissements_survivent_au_json(plan):
    plan.avertissements = ["une incohérence détectée"]
    assert Plan.from_json(plan.to_json()).avertissements == ["une incohérence détectée"]
