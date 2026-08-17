"""Données de récupération : sommeil, VFC, FC de repos, Body Battery, readiness.

Garmin ne documente pas publiquement ces réponses, et leur forme varie selon le
modèle de montre et la version de l'API. Les extracteurs ci-dessous essaient
donc plusieurs chemins et tolèrent l'absence de chaque champ : une donnée
manquante vaut `None`, jamais une exception. Le JSON brut est conservé en base
pour pouvoir corriger l'extraction après coup sans tout re-télécharger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# Statuts de VFC renvoyés par Garmin, traduits pour l'affichage
STATUTS_VFC = {
    "BALANCED": "équilibrée",
    "UNBALANCED": "déséquilibrée",
    "LOW": "basse",
    "POOR": "dégradée",
    "UNKNOWN": "inconnue",
}

NIVEAUX_READINESS = {
    "VERY_HIGH": "très élevée",
    "HIGH": "élevée",
    "MODERATE": "modérée",
    "LOW": "basse",
    "VERY_LOW": "très basse",
    "READY": "prêt",
}


@dataclass
class Wellness:
    """Instantané de récupération pour une journée."""

    jour: date

    # Sommeil
    sommeil_s: float | None = None
    sommeil_profond_s: float | None = None
    sommeil_paradoxal_s: float | None = None
    score_sommeil: int | None = None

    # Variabilité de fréquence cardiaque
    vfc_ms: float | None = None
    vfc_statut: str | None = None
    vfc_baseline_bas: float | None = None
    vfc_baseline_haut: float | None = None

    # Divers
    fc_repos: int | None = None
    body_battery_max: int | None = None
    body_battery_min: int | None = None
    stress_moyen: int | None = None
    readiness_score: int | None = None
    readiness_niveau: str | None = None
    temps_recup_h: float | None = None

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sommeil_h(self) -> float | None:
        return round(self.sommeil_s / 3600, 1) if self.sommeil_s else None

    @property
    def vfc_sous_baseline(self) -> bool | None:
        """Vrai si la VFC de la nuit est passée sous la fourchette habituelle."""
        if self.vfc_ms is None or self.vfc_baseline_bas is None:
            return None
        return self.vfc_ms < self.vfc_baseline_bas

    @property
    def est_vide(self) -> bool:
        """Aucune donnée exploitable ce jour-là."""
        return all(
            v is None
            for v in (
                self.sommeil_s,
                self.vfc_ms,
                self.fc_repos,
                self.readiness_score,
                self.body_battery_max,
                self.stress_moyen,
            )
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "jour": self.jour.isoformat(),
            "sommeil_s": self.sommeil_s,
            "sommeil_profond_s": self.sommeil_profond_s,
            "sommeil_paradoxal_s": self.sommeil_paradoxal_s,
            "score_sommeil": self.score_sommeil,
            "vfc_ms": self.vfc_ms,
            "vfc_statut": self.vfc_statut,
            "vfc_baseline_bas": self.vfc_baseline_bas,
            "vfc_baseline_haut": self.vfc_baseline_haut,
            "fc_repos": self.fc_repos,
            "body_battery_max": self.body_battery_max,
            "body_battery_min": self.body_battery_min,
            "stress_moyen": self.stress_moyen,
            "readiness_score": self.readiness_score,
            "readiness_niveau": self.readiness_niveau,
            "temps_recup_h": self.temps_recup_h,
        }


# --------------------------------------------------------------------------
# Extraction tolérante
# --------------------------------------------------------------------------

def chercher(data: Any, *chemins: str) -> Any:
    """Renvoie la première valeur non nulle trouvée parmi plusieurs chemins pointés.

    `chercher(d, "dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds")` essaie
    les deux emplacements et renvoie None si aucun ne donne de valeur.
    """
    for chemin in chemins:
        courant = data
        for cle in chemin.split("."):
            if isinstance(courant, dict):
                courant = courant.get(cle)
            elif isinstance(courant, list) and cle.isdigit():
                index = int(cle)
                courant = courant[index] if index < len(courant) else None
            else:
                courant = None
            if courant is None:
                break
        if courant is not None:
            return courant
    return None


def _nombre(valeur: Any) -> float | None:
    if valeur is None or isinstance(valeur, bool) or valeur == "":
        return None
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _entier(valeur: Any) -> int | None:
    n = _nombre(valeur)
    return int(round(n)) if n is not None else None


MINUTES_PAR_HEURE = 60
BODY_BATTERY_MAX = 100  # l'échelle Garmin va de 0 à 100


def _niveau_body_battery(ligne: Any) -> int | None:
    """Extrait le niveau de batterie d'une ligne de `bodyBatteryValuesArray`.

    La forme varie selon la version de l'API : `[horodatage, niveau]` sur
    certaines, `[horodatage, "MEASURED", niveau]` sur d'autres. On ignore donc
    la position et on retient la dernière valeur numérique plausible de la
    ligne — l'horodatage, en millisecondes, dépasse largement l'échelle 0-100.
    """
    if not isinstance(ligne, (list, tuple)):
        return None
    for valeur in reversed(ligne):
        niveau = _entier(valeur)
        if niveau is not None and 0 <= niveau <= BODY_BATTERY_MAX:
            return niveau
    return None


def _premier_element(data: Any) -> Any:
    """Certains points d'API renvoient une liste d'un seul élément."""
    if isinstance(data, list):
        return data[0] if data else None
    return data


def depuis_garmin(
    jour: date,
    sommeil: Any = None,
    vfc: Any = None,
    readiness: Any = None,
    body_battery: Any = None,
    stats: Any = None,
) -> Wellness:
    """Assemble une journée de récupération à partir des réponses Garmin brutes."""
    w = Wellness(jour=jour)
    brut: dict[str, Any] = {}

    if sommeil:
        brut["sommeil"] = sommeil
        w.sommeil_s = _nombre(
            chercher(sommeil, "dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds")
        )
        w.sommeil_profond_s = _nombre(
            chercher(sommeil, "dailySleepDTO.deepSleepSeconds", "deepSleepSeconds")
        )
        w.sommeil_paradoxal_s = _nombre(
            chercher(sommeil, "dailySleepDTO.remSleepSeconds", "remSleepSeconds")
        )
        w.score_sommeil = _entier(
            chercher(
                sommeil,
                "dailySleepDTO.sleepScores.overall.value",
                "sleepScores.overall.value",
                "dailySleepDTO.sleepQualityTypePK",
            )
        )
        w.fc_repos = _entier(chercher(sommeil, "restingHeartRate"))

    if vfc:
        brut["vfc"] = vfc
        w.vfc_ms = _nombre(
            chercher(vfc, "hrvSummary.lastNightAvg", "lastNightAvg", "hrvSummary.weeklyAvg")
        )
        statut = chercher(vfc, "hrvSummary.status", "status")
        if statut:
            w.vfc_statut = STATUTS_VFC.get(str(statut).upper(), str(statut).lower())
        w.vfc_baseline_bas = _nombre(
            chercher(vfc, "hrvSummary.baseline.balancedLow", "baseline.balancedLow")
        )
        w.vfc_baseline_haut = _nombre(
            chercher(vfc, "hrvSummary.baseline.balancedUpper", "baseline.balancedUpper")
        )

    if readiness:
        element = _premier_element(readiness)
        if element:
            brut["readiness"] = element
            w.readiness_score = _entier(chercher(element, "score"))
            niveau = chercher(element, "level")
            if niveau:
                w.readiness_niveau = NIVEAUX_READINESS.get(
                    str(niveau).upper(), str(niveau).lower()
                )
            # Garmin exprime `recoveryTime` en minutes sur ce point d'API.
            # Aucune heuristique ici : une conversion conditionnelle rendrait la
            # valeur non monotone (40 → 40 h mais 90 → 1,5 h), ce qui est pire
            # qu'une unité éventuellement fausse mais cohérente. À confirmer sur
            # un vrai compte ; le JSON brut est conservé pour pouvoir rectifier.
            recup = _nombre(chercher(element, "recoveryTime"))
            if recup is not None:
                w.temps_recup_h = round(recup / MINUTES_PAR_HEURE, 1)
            if w.score_sommeil is None:
                w.score_sommeil = _entier(chercher(element, "sleepScore"))

    if body_battery:
        element = _premier_element(body_battery)
        if element:
            brut["body_battery"] = element
            niveaux = [
                niveau
                for v in (chercher(element, "bodyBatteryValuesArray") or [])
                if (niveau := _niveau_body_battery(v)) is not None
            ]
            if niveaux:
                w.body_battery_max = max(niveaux)
                w.body_battery_min = min(niveaux)
            else:
                w.body_battery_max = _entier(chercher(element, "charged"))
                w.body_battery_min = _entier(chercher(element, "drained"))

    if stats:
        brut["stats"] = stats
        if w.fc_repos is None:
            w.fc_repos = _entier(chercher(stats, "restingHeartRate"))
        w.stress_moyen = _entier(
            chercher(stats, "averageStressLevel", "avgStressLevel")
        )

    w.raw = brut
    return w


def from_row(row: Any) -> Wellness:
    """Reconstruit une journée depuis une ligne de la base."""
    return Wellness(
        jour=date.fromisoformat(row["jour"]),
        sommeil_s=row["sommeil_s"],
        sommeil_profond_s=row["sommeil_profond_s"],
        sommeil_paradoxal_s=row["sommeil_paradoxal_s"],
        score_sommeil=row["score_sommeil"],
        vfc_ms=row["vfc_ms"],
        vfc_statut=row["vfc_statut"],
        vfc_baseline_bas=row["vfc_baseline_bas"],
        vfc_baseline_haut=row["vfc_baseline_haut"],
        fc_repos=row["fc_repos"],
        body_battery_max=row["body_battery_max"],
        body_battery_min=row["body_battery_min"],
        stress_moyen=row["stress_moyen"],
        readiness_score=row["readiness_score"],
        readiness_niveau=row["readiness_niveau"],
        temps_recup_h=row["temps_recup_h"],
    )
