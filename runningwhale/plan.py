"""Plan d'entraînement structuré : modèle, export ICS, envoi vers la montre Garmin.

Le plan est produit par le coach sous forme de JSON contraint par un schéma
(cf. `SCHEMA_PLAN`), ce qui permet de le stocker, de le suivre et de l'exporter
au lieu de le laisser en prose libre.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

# Types de séance reconnus. Le coach doit s'y tenir (contrainte du schéma JSON).
TYPES_SEANCE = [
    "endurance",
    "sortie_longue",
    "seuil",
    "vma",
    "cotes",
    "fartlek",
    "recuperation",
    "renforcement",
    "repos",
    "course",
]

# Schéma imposé à la réponse du modèle (structured outputs).
# Toutes les propriétés sont requises ; les valeurs facultatives sont nullables.
SCHEMA_PLAN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "titre": {"type": "string", "description": "Titre court de la semaine"},
        "focus": {
            "type": "string",
            "description": "L'objectif principal de la semaine, en une phrase",
        },
        "resume": {
            "type": "string",
            "description": "Justification du plan au regard de la forme actuelle et de l'objectif",
        },
        "seances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date de la séance au format AAAA-MM-JJ",
                    },
                    "titre": {"type": "string"},
                    "type": {"type": "string", "enum": TYPES_SEANCE},
                    "distance_km": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Distance totale prévue, null si non pertinent",
                    },
                    "duree_min": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Durée totale prévue en minutes",
                    },
                    "objectif": {
                        "type": "string",
                        "description": "Ce que la séance cherche à développer",
                    },
                    "consignes": {
                        "type": "string",
                        "description": "Consignes d'exécution en clair pour l'athlète",
                    },
                    "etapes": {
                        "type": "array",
                        "description": "Découpage de la séance, du plus simple au fractionné",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "enum": [
                                        "echauffement",
                                        "effort",
                                        "recuperation",
                                        "retour_au_calme",
                                    ],
                                },
                                "repetitions": {
                                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                                    "description": "Nombre de répétitions, null ou 1 si bloc unique",
                                },
                                "duree_min": {
                                    "anyOf": [{"type": "number"}, {"type": "null"}]
                                },
                                "distance_km": {
                                    "anyOf": [{"type": "number"}, {"type": "null"}]
                                },
                                "allure": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "description": "Allure cible au format m:ss/km, ex 4:35/km",
                                },
                                "libelle": {"type": "string"},
                            },
                            "required": [
                                "role",
                                "repetitions",
                                "duree_min",
                                "distance_km",
                                "allure",
                                "libelle",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "date",
                    "titre",
                    "type",
                    "distance_km",
                    "duree_min",
                    "objectif",
                    "consignes",
                    "etapes",
                ],
                "additionalProperties": False,
            },
        },
        "points_de_vigilance": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Risques identifiés et signaux à surveiller",
        },
        "volume_total_km": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    },
    "required": [
        "titre",
        "focus",
        "resume",
        "seances",
        "points_de_vigilance",
        "volume_total_km",
    ],
    "additionalProperties": False,
}


@dataclass
class Etape:
    role: str
    libelle: str
    repetitions: int | None = None
    duree_min: float | None = None
    distance_km: float | None = None
    allure: str | None = None

    @property
    def duree_s(self) -> float | None:
        return self.duree_min * 60 if self.duree_min else None

    def resume(self) -> str:
        morceaux = []
        if self.repetitions and self.repetitions > 1:
            morceaux.append(f"{self.repetitions} ×")
        if self.distance_km:
            morceaux.append(f"{self.distance_km:g} km")
        elif self.duree_min:
            morceaux.append(f"{self.duree_min:g} min")
        if self.allure:
            morceaux.append(f"@ {self.allure}")
        detail = " ".join(morceaux)
        return f"{self.libelle} — {detail}" if detail else self.libelle


@dataclass
class Seance:
    date: date
    titre: str
    type: str
    objectif: str = ""
    consignes: str = ""
    distance_km: float | None = None
    duree_min: float | None = None
    etapes: list[Etape] = field(default_factory=list)
    # Suivi d'exécution, renseigné après coup
    realisee: bool = False
    activity_id: str | None = None

    @property
    def est_repos(self) -> bool:
        return self.type == "repos"

    @property
    def duree_estimee_s(self) -> float:
        if self.duree_min:
            return self.duree_min * 60
        total = sum(
            (e.duree_s or 0) * max(e.repetitions or 1, 1) for e in self.etapes
        )
        return total or 3600.0


@dataclass
class Plan:
    titre: str
    focus: str
    resume: str
    seances: list[Seance]
    points_de_vigilance: list[str] = field(default_factory=list)
    volume_total_km: float | None = None
    # Incohérences détectées après coup sur la réponse du modèle (cf. controler_plan).
    avertissements: list[str] = field(default_factory=list)
    genere_le: datetime = field(default_factory=datetime.now)

    @property
    def debut(self) -> date:
        return min((s.date for s in self.seances), default=date.today())

    @property
    def fin(self) -> date:
        return max((s.date for s in self.seances), default=date.today())

    @property
    def volume_calcule_km(self) -> float:
        return round(sum(s.distance_km or 0 for s in self.seances), 1)

    # ---- sérialisation ----

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Plan":
        """Construit un plan depuis la réponse JSON du coach."""
        seances = []
        for s in payload.get("seances", []):
            seances.append(
                Seance(
                    date=_parse_date(s.get("date")),
                    titre=s.get("titre", ""),
                    type=s.get("type", "endurance"),
                    objectif=s.get("objectif", "") or "",
                    consignes=s.get("consignes", "") or "",
                    distance_km=s.get("distance_km"),
                    duree_min=s.get("duree_min"),
                    etapes=[
                        Etape(
                            role=e.get("role", "effort"),
                            libelle=e.get("libelle", ""),
                            repetitions=e.get("repetitions"),
                            duree_min=e.get("duree_min"),
                            distance_km=e.get("distance_km"),
                            allure=e.get("allure"),
                        )
                        for e in s.get("etapes", []) or []
                    ],
                )
            )
        seances.sort(key=lambda s: s.date)
        return cls(
            titre=payload.get("titre", "Semaine d'entraînement"),
            focus=payload.get("focus", ""),
            resume=payload.get("resume", ""),
            seances=seances,
            points_de_vigilance=payload.get("points_de_vigilance", []) or [],
            volume_total_km=payload.get("volume_total_km"),
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["genere_le"] = self.genere_le.isoformat()
        for s in data["seances"]:
            s["date"] = s["date"].isoformat()
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, txt: str) -> "Plan":
        data = json.loads(txt)
        plan = cls.from_payload(data)
        for brut, seance in zip(data.get("seances", []), plan.seances):
            seance.realisee = brut.get("realisee", False)
            seance.activity_id = brut.get("activity_id")
        plan.avertissements = data.get("avertissements", []) or []
        if data.get("genere_le"):
            plan.genere_le = datetime.fromisoformat(data["genere_le"])
        return plan


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date.today()


# --------------------------------------------------------------------------
# Contrôles de vraisemblance sur la réponse du modèle
# --------------------------------------------------------------------------

FACTEUR_VOLUME_SUSPECT = 1.5  # plan > 150 % du plus gros volume récent : suspect
MARGE_VMA = 1.05              # tolérance avant de déclarer une allure sur-VMA


def controler_plan(
    plan: Plan,
    debut: date | None = None,
    vma_kmh: float | None = None,
    volume_recent_km: float | None = None,
) -> list[str]:
    """Contrôles de bon sens sur un plan produit par le modèle.

    Le schéma JSON contraint la forme, pas le sens : rien n'empêche le modèle
    de dater une séance hors de la semaine demandée, de dépasser largement le
    volume que l'athlète encaisse, de prescrire une allure plus rapide que sa
    VMA ou de mettre des kilomètres sur un jour de repos. Sur le modèle du
    signalement de volume du rapport, on avertit sans rejeter : le plan reste
    utilisable, mais l'athlète est prévenu.
    """
    avertissements: list[str] = []
    aujourd_hui = date.today()

    for s in plan.seances:
        if debut is not None and not (debut <= s.date <= debut + timedelta(days=6)):
            avertissements.append(
                f"la séance « {s.titre} » est datée du {s.date:%d/%m/%Y}, hors de la"
                f" semaine demandée ({debut:%d/%m} – {debut + timedelta(days=6):%d/%m})"
            )
        elif s.date < aujourd_hui:
            avertissements.append(
                f"la séance « {s.titre} » du {s.date:%d/%m/%Y} est déjà passée"
                " au moment de la génération du plan"
            )

        if s.est_repos and (
            s.distance_km
            or s.duree_min
            or any(e.distance_km or e.duree_min for e in s.etapes)
        ):
            avertissements.append(
                f"la séance du {s.date:%d/%m} est marquée « repos » mais porte une"
                " distance ou une durée — un jour de repos ne se court pas"
            )

    if vma_kmh and vma_kmh > 0:
        vitesse_vma = vma_kmh / 3.6  # m/s
        for s in plan.seances:
            for e in s.etapes:
                vitesse = allure_vers_ms(e.allure)
                if vitesse and vitesse > vitesse_vma * MARGE_VMA:
                    avertissements.append(
                        f"l'allure {e.allure} demandée le {s.date:%d/%m}"
                        f" (« {s.titre} ») est plus rapide que la VMA estimée"
                        f" ({vma_kmh:g} km/h) — probablement intenable"
                    )

    if volume_recent_km and volume_recent_km > 0:
        calcule = plan.volume_calcule_km
        if calcule > volume_recent_km * FACTEUR_VOLUME_SUSPECT:
            avertissements.append(
                f"le volume planifié ({calcule:g} km) dépasse de plus de"
                f" {round((FACTEUR_VOLUME_SUSPECT - 1) * 100)} % le plus gros volume"
                f" hebdomadaire des 4 dernières semaines ({volume_recent_km:g} km)"
            )

    return avertissements


# --------------------------------------------------------------------------
# Export calendrier (ICS)
# --------------------------------------------------------------------------

def to_ics(plan: Plan, heure: time = time(18, 0)) -> str:
    """Génère un calendrier ICS importable dans Google Agenda, Apple Calendrier, etc."""
    lignes = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RunningWhale//Coach//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_echapper(plan.titre)}",
    ]

    for i, seance in enumerate(plan.seances, start=1):
        if seance.est_repos:
            continue
        debut = datetime.combine(seance.date, heure)
        fin = debut + timedelta(seconds=seance.duree_estimee_s)
        description = _description_ics(seance)
        uid = f"runningwhale-{plan.debut:%Y%m%d}-{i}@runningwhale"

        lignes += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{plan.genere_le:%Y%m%dT%H%M%S}",
            f"DTSTART:{debut:%Y%m%dT%H%M%S}",
            f"DTEND:{fin:%Y%m%dT%H%M%S}",
            f"SUMMARY:{_echapper(seance.titre)}",
            f"DESCRIPTION:{_echapper(description)}",
            "CATEGORIES:Entraînement",
            "BEGIN:VALARM",
            "TRIGGER:-PT2H",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_echapper(seance.titre)}",
            "END:VALARM",
            "END:VEVENT",
        ]

    lignes.append("END:VCALENDAR")
    # RFC 5545 impose des fins de ligne CRLF.
    return "\r\n".join(_plier(l) for l in lignes) + "\r\n"


def _description_ics(seance: Seance) -> str:
    blocs = []
    if seance.objectif:
        blocs.append(f"Objectif : {seance.objectif}")
    if seance.etapes:
        blocs.append("Déroulé :")
        blocs += [f"  • {e.resume()}" for e in seance.etapes]
    if seance.consignes:
        blocs.append(f"Consignes : {seance.consignes}")
    return "\n".join(blocs)


def _echapper(txt: str) -> str:
    return (
        (txt or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _plier(ligne: str) -> str:
    """Repli des lignes à 75 octets, comme l'exige la RFC 5545."""
    if len(ligne.encode("utf-8")) <= 75:
        return ligne
    morceaux, courant = [], ""
    for caractere in ligne:
        if len((courant + caractere).encode("utf-8")) > 73:
            morceaux.append(courant)
            courant = " " + caractere
        else:
            courant += caractere
    morceaux.append(courant)
    return "\r\n".join(morceaux)


# --------------------------------------------------------------------------
# Export vers la montre Garmin
# --------------------------------------------------------------------------

ROLE_VERS_STEP = {
    "echauffement": (1, "warmup"),
    "effort": (3, "interval"),
    "recuperation": (4, "recovery"),
    "retour_au_calme": (2, "cooldown"),
}
MARGE_ALLURE = 0.05  # ±5 % autour de l'allure cible, pour une fourchette réaliste


def allure_vers_ms(allure: str | None) -> float | None:
    """'4:35/km' -> vitesse en m/s. Renvoie None si non interprétable."""
    if not allure:
        return None
    m = re.search(r"(\d{1,2})\s*[:'’]\s*(\d{2})", str(allure))
    if not m:
        return None
    s_km = int(m.group(1)) * 60 + int(m.group(2))
    if s_km <= 0:
        return None
    return 1000.0 / s_km


def seance_vers_workout(seance: Seance) -> Any:
    """Convertit une séance en `RunningWorkout` prête à être téléversée.

    Nécessite `pydantic` (extra `garminconnect[workout]`).
    """
    from garminconnect.workout import RunningWorkout, WorkoutSegment
    from garminconnect.workout import ExecutableStep, RepeatGroup

    steps: list[Any] = []
    ordre = 1

    for etape in seance.etapes:
        repetitions = max(etape.repetitions or 1, 1)
        step = _etape_vers_step(ExecutableStep, etape, ordre)
        if step is None:
            continue

        if repetitions > 1:
            steps.append(
                RepeatGroup(
                    stepOrder=ordre,
                    numberOfIterations=repetitions,
                    workoutSteps=[step],
                    stepType={"stepTypeId": 6, "stepTypeKey": "repeat"},
                )
            )
        else:
            steps.append(step)
        ordre += 1

    if not steps:
        # Séance sans découpage : un bloc unique sur la durée totale.
        steps = [
            ExecutableStep(
                stepOrder=1,
                stepType={"stepTypeId": 3, "stepTypeKey": "interval"},
                endCondition={"conditionTypeId": 2, "conditionTypeKey": "time"},
                endConditionValue=seance.duree_estimee_s,
                targetType={"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            )
        ]

    return RunningWorkout(
        workoutName=seance.titre[:80],
        description=(seance.objectif or seance.consignes or "")[:1024],
        estimatedDurationInSecs=int(seance.duree_estimee_s),
        sportType={"sportTypeId": 1, "sportTypeKey": "running"},
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 1, "sportTypeKey": "running"},
                workoutSteps=steps,
            )
        ],
    )


def _etape_vers_step(ExecutableStep: Any, etape: Etape, ordre: int) -> Any | None:
    type_id, type_key = ROLE_VERS_STEP.get(etape.role, (3, "interval"))

    if etape.distance_km:
        condition = {"conditionTypeId": 1, "conditionTypeKey": "distance"}
        valeur = etape.distance_km * 1000.0
    elif etape.duree_min:
        condition = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        valeur = etape.duree_min * 60.0
    else:
        # Sans durée ni distance, la montre attend un appui sur le bouton tour.
        condition = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
        valeur = None

    step = ExecutableStep(
        stepOrder=ordre,
        stepType={"stepTypeId": type_id, "stepTypeKey": type_key},
        endCondition=condition,
        endConditionValue=valeur,
        targetType={"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
    )

    vitesse = allure_vers_ms(etape.allure)
    if vitesse:
        # Garmin attend une fourchette de vitesse en m/s pour une cible d'allure.
        step.targetType = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
        step.targetValueOne = round(vitesse * (1 - MARGE_ALLURE), 3)
        step.targetValueTwo = round(vitesse * (1 + MARGE_ALLURE), 3)

    return step


def pousser_vers_garmin(api: Any, plan: Plan, planifier: bool = True) -> list[dict[str, Any]]:
    """Téléverse chaque séance du plan dans Garmin Connect, et la programme.

    Renvoie la liste des résultats, un par séance (avec l'erreur le cas échéant).
    """
    resultats: list[dict[str, Any]] = []
    for seance in plan.seances:
        if seance.est_repos:
            continue
        try:
            workout = seance_vers_workout(seance)
            reponse = api.upload_running_workout(workout)
            workout_id = reponse.get("workoutId") if isinstance(reponse, dict) else None
            if planifier and workout_id:
                api.schedule_workout(workout_id, seance.date.isoformat())
            resultats.append(
                {"seance": seance.titre, "date": seance.date, "workout_id": workout_id,
                 "erreur": None}
            )
        except Exception as exc:  # noqa: BLE001 — on rapporte séance par séance
            resultats.append(
                {"seance": seance.titre, "date": seance.date, "workout_id": None,
                 "erreur": str(exc)}
            )
    return resultats


# --------------------------------------------------------------------------
# Suivi d'adhérence
# --------------------------------------------------------------------------

@dataclass
class Adherence:
    """Comparaison entre ce qui était prévu et ce qui a été couru."""

    prevues: int
    realisees: int
    km_prevus: float
    km_realises: float
    manquees: list[Seance] = field(default_factory=list)
    hors_plan: list[Any] = field(default_factory=list)

    @property
    def taux(self) -> float:
        return round(self.realisees / self.prevues * 100, 1) if self.prevues else 0.0

    @property
    def ecart_km(self) -> float:
        return round(self.km_realises - self.km_prevus, 1)


def rapprocher(plan: Plan, activities: list[Any], tolerance_jours: int = 1) -> Adherence:
    """Associe chaque séance planifiée à l'activité réellement courue.

    Une séance est considérée réalisée s'il existe une course dans la fenêtre
    de tolérance autour de la date prévue. Chaque activité n'est appariée qu'une fois.
    """
    courses = sorted(
        [a for a in activities if getattr(a, "est_course", False)],
        key=lambda a: a.debut,
    )
    utilisees: set[str] = set()

    for seance in plan.seances:
        if seance.est_repos:
            continue
        candidates = [
            a
            for a in courses
            if a.activity_id not in utilisees
            and abs((a.debut.date() - seance.date).days) <= tolerance_jours
        ]
        if not candidates:
            continue
        # La plus proche en date, puis en distance de ce qui était prévu.
        cible = seance.distance_km or 0
        meilleure = min(
            candidates,
            key=lambda a: (
                abs((a.debut.date() - seance.date).days),
                abs(a.distance_km - cible) if cible else 0,
            ),
        )
        seance.realisee = True
        seance.activity_id = meilleure.activity_id
        utilisees.add(meilleure.activity_id)

    planifiees = [s for s in plan.seances if not s.est_repos]
    faites = [s for s in planifiees if s.realisee]
    dans_periode = [
        a for a in courses if plan.debut <= a.debut.date() <= plan.fin
    ]

    return Adherence(
        prevues=len(planifiees),
        realisees=len(faites),
        km_prevus=round(sum(s.distance_km or 0 for s in planifiees), 1),
        km_realises=round(sum(a.distance_km for a in dans_periode), 1),
        manquees=[s for s in planifiees if not s.realisee and s.date <= date.today()],
        hors_plan=[a for a in dans_periode if a.activity_id not in utilisees],
    )
