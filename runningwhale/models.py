"""Représentation normalisée d'une activité, indépendante du format Garmin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Lap:
    """Un tour / segment d'une activité."""

    index: int
    distance_m: float
    duree_s: float
    fc_moy: int | None = None
    allure_s_km: float | None = None
    denivele_pos_m: float | None = None


@dataclass
class Activity:
    """Une séance. Les champs optionnels reflètent ce que la montre a mesuré."""

    activity_id: str
    debut: datetime
    type: str
    nom: str = ""
    distance_m: float = 0.0
    duree_s: float = 0.0
    duree_mouvement_s: float | None = None
    denivele_pos_m: float | None = None
    denivele_neg_m: float | None = None
    fc_moy: int | None = None
    fc_max: int | None = None
    cadence_moy: int | None = None
    puissance_moy: int | None = None
    calories: int | None = None
    vo2max: float | None = None
    training_effect_aerobie: float | None = None
    training_effect_anaerobie: float | None = None
    temperature_c: float | None = None
    laps: list[Lap] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- propriétés dérivées ----

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0

    @property
    def duree_effective_s(self) -> float:
        """Durée de mouvement si disponible, sinon durée totale."""
        return self.duree_mouvement_s or self.duree_s

    @property
    def allure_s_km(self) -> float | None:
        """Allure moyenne en secondes par kilomètre."""
        if self.distance_km <= 0 or self.duree_effective_s <= 0:
            return None
        return self.duree_effective_s / self.distance_km

    @property
    def vitesse_kmh(self) -> float | None:
        if self.distance_km <= 0 or self.duree_effective_s <= 0:
            return None
        return self.distance_km / (self.duree_effective_s / 3600.0)

    @property
    def est_course(self) -> bool:
        return "running" in self.type.lower() or "trail" in self.type.lower()

    def to_row(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "debut": self.debut.isoformat(),
            "type": self.type,
            "nom": self.nom,
            "distance_m": self.distance_m,
            "duree_s": self.duree_s,
            "duree_mouvement_s": self.duree_mouvement_s,
            "denivele_pos_m": self.denivele_pos_m,
            "denivele_neg_m": self.denivele_neg_m,
            "fc_moy": self.fc_moy,
            "fc_max": self.fc_max,
            "cadence_moy": self.cadence_moy,
            "puissance_moy": self.puissance_moy,
            "calories": self.calories,
            "vo2max": self.vo2max,
            "training_effect_aerobie": self.training_effect_aerobie,
            "training_effect_anaerobie": self.training_effect_anaerobie,
            "temperature_c": self.temperature_c,
        }


def _f(value: Any) -> float | None:
    """Conversion tolérante en flottant (Garmin renvoie parfois None ou des chaînes)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(round(f)) if f is not None else None


def activity_from_garmin(data: dict[str, Any]) -> Activity:
    """Convertit le JSON d'une activité Garmin Connect en `Activity`."""
    debut_txt = (
        data.get("startTimeLocal")
        or data.get("startTimeGMT")
        or data.get("summaryDTO", {}).get("startTimeLocal")
        or ""
    )
    debut = _parse_garmin_datetime(debut_txt)

    type_key = ""
    type_obj = data.get("activityType")
    if isinstance(type_obj, dict):
        type_key = type_obj.get("typeKey") or ""
    elif isinstance(type_obj, str):
        type_key = type_obj

    return Activity(
        activity_id=str(data.get("activityId") or data.get("activity_id") or ""),
        debut=debut,
        type=type_key or "unknown",
        nom=data.get("activityName") or "",
        distance_m=_f(data.get("distance")) or 0.0,
        duree_s=_f(data.get("duration")) or 0.0,
        duree_mouvement_s=_f(data.get("movingDuration")),
        denivele_pos_m=_f(data.get("elevationGain")),
        denivele_neg_m=_f(data.get("elevationLoss")),
        fc_moy=_i(data.get("averageHR")),
        fc_max=_i(data.get("maxHR")),
        cadence_moy=_i(data.get("averageRunningCadenceInStepsPerMinute")),
        puissance_moy=_i(data.get("avgPower")),
        calories=_i(data.get("calories")),
        vo2max=_f(data.get("vO2MaxValue")),
        training_effect_aerobie=_f(data.get("aerobicTrainingEffect")),
        training_effect_anaerobie=_f(data.get("anaerobicTrainingEffect")),
        temperature_c=_f(data.get("minTemperature")),
        raw=data,
    )


FORMATS_DATE_GARMIN = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d",
)


def _parse_garmin_datetime(txt: str) -> datetime:
    """Interprète une date Garmin, ou lève si aucun format ne convient.

    Retomber sur une date sentinelle (1970) serait pire que d'échouer : une
    activité ainsi datée entre en base et fausse silencieusement toutes les
    fenêtres de calcul — charge, volumes hebdomadaires, records. Mieux vaut que
    l'appelant écarte l'activité et le signale.
    """
    for fmt in FORMATS_DATE_GARMIN:
        try:
            return datetime.strptime(txt, fmt)
        except (ValueError, TypeError):
            continue
    raise ValueError(f"date Garmin illisible : {txt!r}")


def laps_from_garmin(splits: dict[str, Any]) -> list[Lap]:
    """Extrait les tours depuis la réponse `get_activity_splits`."""
    laps: list[Lap] = []
    for i, item in enumerate(splits.get("lapDTOs") or [], start=1):
        distance = _f(item.get("distance")) or 0.0
        duree = _f(item.get("duration")) or 0.0
        allure = (duree / (distance / 1000.0)) if distance > 0 and duree > 0 else None
        laps.append(
            Lap(
                index=i,
                distance_m=distance,
                duree_s=duree,
                fc_moy=_i(item.get("averageHR")),
                allure_s_km=allure,
                denivele_pos_m=_f(item.get("elevationGain")),
            )
        )
    return laps
