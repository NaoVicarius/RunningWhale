"""Import d'activités depuis des fichiers exportés, sans réseau ni compte.

Garmin refuse l'authentification depuis certaines adresses — une IP de centre
de données part en quota puis en CAPTCHA, et aucun mot de passe n'y change rien
(voir `garmin._message_ip_refusee`). Les données, elles, s'exportent à la main.
Cette couche lit ce que Garmin sait produire et le ramène au même `Activity`
que la synchronisation en ligne, de sorte que tout le reste du coach — charge,
forme, allures, débriefs — ne voit aucune différence.

Quatre formats, parce qu'ils viennent de quatre endroits différents :

* l'**export complet du compte** (« Export Your Data »), un zip contenant des
  JSON `summarizedActivities` : tout l'historique en une seule demande ;
* les **TCX** exportés activité par activité : plus pauvres en résumé, mais ils
  portent les tours, donc la répartition d'intensité et la dérive cardiaque ;
* les **FIT** d'origine de la montre : les plus riches, via `fitparse` ;
* les **GPX**, en dernier recours : une trace, sans fréquence cardiaque.
"""

from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .db import Database
from .models import Activity, Lap, activity_from_garmin

SUFFIXES_CONNUS = (".zip", ".json", ".tcx", ".gpx", ".fit", ".csv")

# Deux activités qui commencent à moins de cette distance l'une de l'autre sont
# la même séance vue par deux exports (le zip du compte et le TCX de la montre,
# typiquement). Les horodatages ne coïncident pas à la seconde d'un format à
# l'autre — l'un date le départ du chrono, l'autre le premier point enregistré.
TOLERANCE_DOUBLON = timedelta(minutes=2)

# Garde-fou de vraisemblance. Les exports de Garmin ne partagent pas leurs
# unités : l'API Connect donne des mètres et des secondes, l'export du compte
# des centimètres et des millisecondes. Se tromper d'échelle ne produit pas une
# erreur, mais une base fausse — un 10 km devenu 100 km, et toute la charge avec.
# Mieux vaut refuser une activité que la laisser entrer à la mauvaise échelle.
VITESSE_MAX_PLAUSIBLE_KMH = 80.0
DUREE_MAX_PLAUSIBLE_S = 48 * 3600.0


class ImportError_(ValueError):
    """Fichier illisible ou format non reconnu."""


@dataclass
class ResultatImport:
    """Ce qu'un import a produit, y compris ce qu'il a refusé."""

    nouvelles: int = 0
    mises_a_jour: int = 0
    doublons: int = 0  # même séance déjà en base, vue par un autre export
    exclues: int = 0  # types écartés à la demande (voir `importer(exclure=…)`)
    ignorees: list[tuple[str, str]] = field(default_factory=list)  # (source, raison)
    fichiers_lus: int = 0

    @property
    def total(self) -> int:
        return self.nouvelles + self.mises_a_jour


# ---------------------------------------------------------------- utilitaires


def _f(valeur: Any) -> float | None:
    try:
        if valeur is None or valeur == "":
            return None
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _i(valeur: Any) -> int | None:
    f = _f(valeur)
    return int(round(f)) if f is not None else None


def _texte(noeud: ET.Element | None) -> str:
    return (noeud.text or "").strip() if noeud is not None else ""


def _sans_ns(balise: str) -> str:
    """`{http://…}Lap` → `Lap`. Les exports Garmin varient de namespace."""
    return balise.rsplit("}", 1)[-1]


def _trouver(parent: ET.Element, *chemin: str) -> ET.Element | None:
    """Descend un chemin de balises en ignorant les namespaces."""
    courant: ET.Element | None = parent
    for nom in chemin:
        if courant is None:
            return None
        courant = next(
            (e for e in courant if _sans_ns(e.tag) == nom),
            None,
        )
    return courant


def _identifiant_local(debut: datetime, prefixe: str = "local") -> str:
    """Identifiant stable pour un format qui n'en porte pas.

    Dérivé de l'instant de départ, à la minute : le même run exporté en TCX et
    en FIT retombe sur le même identifiant, donc sur une seule ligne en base.
    """
    return f"{prefixe}-{debut.strftime('%Y%m%dT%H%M')}"


def _plausible(activity: Activity) -> str | None:
    """Renvoie la raison du refus si l'activité est hors de toute échelle."""
    if activity.duree_s > DUREE_MAX_PLAUSIBLE_S:
        return f"durée invraisemblable ({activity.duree_s / 3600:.0f} h)"
    vitesse = activity.vitesse_kmh
    if vitesse is not None and vitesse > VITESSE_MAX_PLAUSIBLE_KMH:
        return f"vitesse invraisemblable ({vitesse:.0f} km/h) — unités douteuses"
    if activity.distance_m < 0 or activity.duree_s < 0:
        return "distance ou durée négative"
    return None


# ------------------------------------------------------- export complet (JSON)


def _est_export_de_compte(data: dict[str, Any]) -> bool:
    """Distingue l'export du compte de la réponse de l'API Connect.

    On tranche sur la **forme**, pas sur les valeurs : l'export du compte date
    par `beginTimestamp` (epoch en millisecondes) et nomme le type d'activité
    par une chaîne, là où l'API Connect donne `startTimeLocal` et un objet
    `activityType`. Les unités se déduisent ensuite du schéma reconnu, et le
    garde-fou de vraisemblance rattrape une reconnaissance erronée.
    """
    return "beginTimestamp" in data and "startTimeLocal" not in data


def activite_depuis_export_compte(data: dict[str, Any]) -> Activity:
    """Convertit une entrée `summarizedActivities` de l'export du compte.

    Cet export ne parle ni en mètres ni en secondes : les distances et les
    dénivelés sont en centimètres, les durées en millisecondes, et l'instant de
    départ est un epoch en millisecondes.
    """
    epoch_ms = _f(data.get("beginTimestamp"))
    if epoch_ms is None:
        raise ValueError("activité sans date de départ")
    debut = datetime.fromtimestamp(epoch_ms / 1000.0)

    type_brut = data.get("activityType")
    if isinstance(type_brut, dict):
        type_brut = type_brut.get("typeKey")

    def _cm_en_m(cle: str) -> float | None:
        valeur = _f(data.get(cle))
        return valeur / 100.0 if valeur is not None else None

    def _ms_en_s(cle: str) -> float | None:
        valeur = _f(data.get(cle))
        return valeur / 1000.0 if valeur is not None else None

    return Activity(
        activity_id=str(data.get("activityId") or _identifiant_local(debut, "export")),
        debut=debut,
        type=str(type_brut or "unknown"),
        nom=str(data.get("name") or ""),
        distance_m=_cm_en_m("distance") or 0.0,
        duree_s=_ms_en_s("duration") or 0.0,
        duree_mouvement_s=_ms_en_s("movingDuration"),
        denivele_pos_m=_cm_en_m("elevationGain"),
        denivele_neg_m=_cm_en_m("elevationLoss"),
        fc_moy=_i(data.get("avgHr")),
        fc_max=_i(data.get("maxHr")),
        cadence_moy=_i(data.get("avgRunCadence")),
        puissance_moy=_i(data.get("avgPower")),
        calories=_i(data.get("calories")),
        vo2max=_f(data.get("vO2MaxValue")),
        training_effect_aerobie=_f(data.get("aerobicTrainingEffect")),
        training_effect_anaerobie=_f(data.get("anaerobicTrainingEffect")),
        raw=data,
    )


def _entrees_json(contenu: Any) -> Iterable[dict[str, Any]]:
    """Déplie les emballages du JSON Garmin, quel qu'il soit.

    L'export du compte emballe ses activités dans une liste d'objets portant
    une clé `summarizedActivitiesExport` ; l'API Connect renvoie une liste
    plate. On accepte les deux, et un objet seul.
    """
    if isinstance(contenu, dict):
        contenu = [contenu]
    if not isinstance(contenu, list):
        return []

    entrees: list[dict[str, Any]] = []
    for item in contenu:
        if not isinstance(item, dict):
            continue
        interne = item.get("summarizedActivitiesExport")
        if isinstance(interne, list):
            entrees.extend(e for e in interne if isinstance(e, dict))
        else:
            entrees.append(item)
    return entrees


def lire_json(texte: str) -> list[Activity]:
    try:
        contenu = json.loads(texte)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"JSON illisible : {exc}") from exc

    activites: list[Activity] = []
    for entree in _entrees_json(contenu):
        try:
            if _est_export_de_compte(entree):
                activites.append(activite_depuis_export_compte(entree))
            else:
                activites.append(activity_from_garmin(entree))
        except (ValueError, TypeError):
            # Une entrée illisible ne condamne pas le fichier : l'export d'un
            # compte contient aussi des enregistrements sans date exploitable.
            continue
    return activites


# ------------------------------------------------------------------ TCX / GPX


def lire_tcx(texte: str) -> list[Activity]:
    """Lit un TCX Garmin : résumé reconstruit depuis les tours."""
    try:
        racine = ET.fromstring(texte)
    except ET.ParseError as exc:
        raise ImportError_(f"TCX illisible : {exc}") from exc

    activites: list[Activity] = []
    for noeud in racine.iter():
        if _sans_ns(noeud.tag) != "Activity":
            continue
        sport = noeud.get("Sport", "") or ""
        debut_txt = _texte(_trouver(noeud, "Id"))
        try:
            debut = _parse_iso(debut_txt)
        except ValueError:
            continue

        laps: list[Lap] = []
        distance_totale = duree_totale = 0.0
        calories = 0
        fc_max: int | None = None
        somme_fc_ponderee = 0.0
        for i, lap_node in enumerate(
            (e for e in noeud if _sans_ns(e.tag) == "Lap"), start=1
        ):
            duree = _f(_texte(_trouver(lap_node, "TotalTimeSeconds"))) or 0.0
            distance = _f(_texte(_trouver(lap_node, "DistanceMeters"))) or 0.0
            fc_moy = _i(_texte(_trouver(lap_node, "AverageHeartRateBpm", "Value")))
            fc_pic = _i(_texte(_trouver(lap_node, "MaximumHeartRateBpm", "Value")))
            calories += _i(_texte(_trouver(lap_node, "Calories"))) or 0

            distance_totale += distance
            duree_totale += duree
            if fc_pic is not None:
                fc_max = max(fc_max or 0, fc_pic)
            if fc_moy is not None and duree > 0:
                somme_fc_ponderee += fc_moy * duree

            laps.append(
                Lap(
                    index=i,
                    distance_m=distance,
                    duree_s=duree,
                    fc_moy=fc_moy,
                    allure_s_km=(duree / (distance / 1000.0))
                    if distance > 0 and duree > 0
                    else None,
                )
            )

        # Moyenne pondérée par la durée des tours, et non moyenne des tours :
        # un tour de récupération de 30 s ne pèse pas autant qu'un bloc de 10 min.
        fc_moyenne = (
            _i(somme_fc_ponderee / duree_totale) if duree_totale > 0 and somme_fc_ponderee else None
        )
        activites.append(
            Activity(
                activity_id=_identifiant_local(debut),
                debut=debut,
                type=_type_depuis_sport(sport),
                nom="",
                distance_m=distance_totale,
                duree_s=duree_totale,
                fc_moy=fc_moyenne,
                fc_max=fc_max,
                calories=calories or None,
                laps=laps,
                raw={"source": "tcx", "sport": sport},
            )
        )
    return activites


def lire_gpx(texte: str) -> list[Activity]:
    """Lit un GPX : distance calculée depuis la trace, sans fréquence cardiaque."""
    try:
        racine = ET.fromstring(texte)
    except ET.ParseError as exc:
        raise ImportError_(f"GPX illisible : {exc}") from exc

    activites: list[Activity] = []
    for trk in (e for e in racine.iter() if _sans_ns(e.tag) == "trk"):
        points: list[tuple[float, float, datetime | None]] = []
        frequences: list[int] = []
        for pt in (e for e in trk.iter() if _sans_ns(e.tag) == "trkpt"):
            lat, lon = _f(pt.get("lat")), _f(pt.get("lon"))
            if lat is None or lon is None:
                continue
            instant = None
            noeud_temps = next((e for e in pt if _sans_ns(e.tag) == "time"), None)
            if noeud_temps is not None:
                try:
                    instant = _parse_iso(_texte(noeud_temps))
                except ValueError:
                    instant = None
            points.append((lat, lon, instant))
            for sous in pt.iter():
                if _sans_ns(sous.tag) == "hr":
                    fc = _i(_texte(sous))
                    if fc:
                        frequences.append(fc)

        horodates = [p[2] for p in points if p[2] is not None]
        if not horodates:
            continue
        debut, fin = min(horodates), max(horodates)
        distance = sum(
            _haversine_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
            for i in range(1, len(points))
        )
        activites.append(
            Activity(
                activity_id=_identifiant_local(debut),
                debut=debut,
                type=_type_depuis_sport(_texte(_trouver(trk, "type"))),
                nom=_texte(_trouver(trk, "name")),
                distance_m=distance,
                duree_s=(fin - debut).total_seconds(),
                fc_moy=_i(sum(frequences) / len(frequences)) if frequences else None,
                fc_max=max(frequences) if frequences else None,
                raw={"source": "gpx"},
            )
        )
    return activites


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance entre deux points, sur une Terre sphérique (±0,5 %)."""
    rayon = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * rayon * math.asin(math.sqrt(min(1.0, a)))


def _type_depuis_sport(sport: str) -> str:
    """Ramène le vocabulaire TCX/GPX/FIT à celui de Garmin Connect."""
    s = (sport or "").strip().lower()
    if not s:
        return "unknown"
    if "run" in s or s == "course":
        return "trail_running" if "trail" in s else "running"
    if "bik" in s or "cycl" in s or "velo" in s:
        return "cycling"
    if "swim" in s or "nage" in s:
        return "swimming"
    if "walk" in s or "hik" in s or "march" in s:
        return "walking"
    return s


def _parse_iso(txt: str) -> datetime:
    """Interprète un horodatage ISO, avec ou sans `Z`, et le rend local-naïf.

    Le reste du coach travaille en heure locale naïve (c'est ce que Garmin
    Connect renvoie). Un horodatage TCX/GPX est en UTC : on le convertit, sinon
    une sortie de 7 h du matin atterrit à 5 h et change de journée en base.
    """
    brut = (txt or "").strip()
    if not brut:
        raise ValueError("horodatage vide")
    if brut.endswith("Z"):
        brut = brut[:-1] + "+00:00"
    try:
        instant = datetime.fromisoformat(brut)
    except ValueError as exc:
        raise ValueError(f"horodatage illisible : {txt!r}") from exc
    if instant.tzinfo is not None:
        instant = instant.astimezone().replace(tzinfo=None)
    return instant


# -------------------------------------------------------------------- CSV Garmin

# En-têtes de la liste d'activités exportée depuis Garmin Connect. Ils sont
# traduits dans la langue du compte : on accepte les deux variantes courantes,
# et l'absence d'une colonne n'est jamais fatale — le champ reste simplement
# vide, comme pour une montre qui ne l'aurait pas mesuré.
COLONNES_CSV: dict[str, tuple[str, ...]] = {
    "type": ("type d'activité", "activity type"),
    "date": ("date",),
    "titre": ("titre", "title"),
    "distance": ("distance",),
    "calories": ("calories",),
    "duree": ("durée", "time"),
    "fc_moy": ("fréquence cardiaque moyenne", "avg hr"),
    "fc_max": ("fréquence cardiaque maximale", "max hr"),
    "te_aerobie": ("te aérobie", "aerobic te"),
    "cadence": ("cadence de course moyenne", "avg run cadence"),
    "denivele_pos": ("ascension totale", "total ascent"),
    "denivele_neg": ("descente totale", "total descent"),
    "puissance": ("puissance moyenne", "avg power"),
    "temperature": ("température minimale", "min temp"),
    "duree_mouvement": ("temps de déplacement", "moving time"),
}

# Correspondance des types d'activité, du vocabulaire Garmin Connect vers celui
# de l'API (`typeKey`), le seul que connaisse le reste du coach.
TYPES_CSV: tuple[tuple[tuple[str, ...], str], ...] = (
    (("course à pied", "running"), "running"),
    (("trail", "course sur sentier", "trail running"), "trail_running"),
    (("tapis", "treadmill"), "treadmill_running"),
    (("marche", "walking"), "walking"),
    (("randonnée", "hiking"), "hiking"),
    (("vélo", "cyclisme", "cycling", "biking"), "cycling"),
    (("natation", "swimming"), "swimming"),
    (("musculation", "strength"), "strength_training"),
)


def _entete(nom: str) -> str:
    return nom.strip().lower().lstrip("﻿")


def _nombre_csv(brut: Any) -> float | None:
    """Lit un nombre du CSV Garmin, avec ses parasites d'export tableur.

    Trois pièges : `--` marque une mesure absente ; une apostrophe de tête
    (`'-12`) est la protection anti-formule d'Excel ; et la virgule sépare
    tantôt les milliers (`4,404`), tantôt les décimales selon la langue du
    compte. On tranche sur le nombre de chiffres qui la suivent — trois, et
    c'est un séparateur de milliers.
    """
    if brut is None:
        return None
    txt = str(brut).strip().strip('"').lstrip("'").strip()
    if not txt or txt in ("--", "-", "...."):
        return None

    if "." in txt and "," in txt:
        # Le dernier séparateur rencontré porte les décimales.
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", "")
    elif "," in txt:
        avant, _, apres = txt.rpartition(",")
        txt = f"{avant}{apres}" if len(apres) == 3 and apres.isdigit() else f"{avant}.{apres}"

    try:
        return float(txt)
    except ValueError:
        return None


def _duree_csv(brut: Any) -> float | None:
    """Convertit `00:35:20`, `01:05:20.5` ou `35:20` en secondes."""
    if brut is None:
        return None
    txt = str(brut).strip().strip('"')
    if not txt or txt in ("--", "-"):
        return None
    morceaux = txt.split(":")
    if not 1 <= len(morceaux) <= 3:
        return None
    try:
        valeurs = [float(m) for m in morceaux]
    except ValueError:
        return None
    secondes = 0.0
    for valeur in valeurs:  # heures, minutes, secondes — de gauche à droite
        secondes = secondes * 60 + valeur
    return secondes


def _type_csv(libelle: str) -> str:
    bas = (libelle or "").strip().lower()
    if not bas:
        return "unknown"
    for cles, typekey in TYPES_CSV:
        if any(cle in bas for cle in cles):
            return typekey
    return bas.replace(" ", "_")


def lire_csv(texte: str) -> list[Activity]:
    """Lit la liste d'activités exportée depuis Garmin Connect (bouton CSV).

    C'est l'export le plus immédiat — un clic depuis la page des activités —
    mais aussi le plus pauvre : il donne un résumé par séance, sans le détail
    des tours ni la courbe de fréquence cardiaque. La répartition d'intensité
    et la dérive cardiaque s'en trouvent moins fines ; tout le reste (charge,
    CTL/ATL, allures, volumes) se calcule normalement.

    Les distances y sont en **kilomètres**, contrairement à tous les autres
    formats Garmin.
    """
    lignes = texte.splitlines()
    if not lignes:
        return []

    lecteur = csv.DictReader(lignes)
    if not lecteur.fieldnames:
        raise ImportError_("CSV sans en-tête")

    # On indexe les en-têtes une fois, en tolérant la langue du compte.
    index: dict[str, str] = {}
    for colonne in lecteur.fieldnames:
        normalise = _entete(colonne)
        for champ, variantes in COLONNES_CSV.items():
            if champ not in index and any(normalise == v for v in variantes):
                index[champ] = colonne

    if "date" not in index:
        raise ImportError_(
            "CSV non reconnu : aucune colonne de date. Attendu, la liste "
            "d'activités exportée depuis Garmin Connect."
        )

    def champ(ligne: dict[str, str], nom: str) -> Any:
        colonne = index.get(nom)
        return ligne.get(colonne) if colonne else None

    activites: list[Activity] = []
    for ligne in lecteur:
        try:
            debut = _parse_iso(str(champ(ligne, "date") or "").strip().strip('"'))
        except ValueError:
            continue

        distance_km = _nombre_csv(champ(ligne, "distance")) or 0.0
        libelle = str(champ(ligne, "type") or "").strip().strip('"')
        activites.append(
            Activity(
                activity_id=_identifiant_local(debut),
                debut=debut,
                type=_type_csv(libelle),
                nom=str(champ(ligne, "titre") or "").strip().strip('"'),
                distance_m=distance_km * 1000.0,  # le CSV compte en kilomètres
                duree_s=_duree_csv(champ(ligne, "duree")) or 0.0,
                duree_mouvement_s=_duree_csv(champ(ligne, "duree_mouvement")),
                denivele_pos_m=_nombre_csv(champ(ligne, "denivele_pos")),
                denivele_neg_m=_nombre_csv(champ(ligne, "denivele_neg")),
                fc_moy=_i(_nombre_csv(champ(ligne, "fc_moy"))),
                fc_max=_i(_nombre_csv(champ(ligne, "fc_max"))),
                cadence_moy=_i(_nombre_csv(champ(ligne, "cadence"))),
                puissance_moy=_i(_nombre_csv(champ(ligne, "puissance"))),
                calories=_i(_nombre_csv(champ(ligne, "calories"))),
                training_effect_aerobie=_nombre_csv(champ(ligne, "te_aerobie")),
                temperature_c=_nombre_csv(champ(ligne, "temperature")),
                # Le libellé d'origine survit : c'est le mot que l'athlète voit
                # dans Garmin, donc celui qu'il écrira dans `--exclure`.
                raw={"source": "csv", "type_libelle": libelle},
            )
        )
    return activites


# ------------------------------------------------------------------------ FIT


def lire_fit(chemin: Path) -> list[Activity]:
    """Lit un FIT via `fitparse`, le format d'origine de la montre."""
    try:
        from fitparse import FitFile
    except ImportError as exc:  # dépendance optionnelle
        raise ImportError_(
            "Les fichiers .fit demandent `fitparse` : `pip install fitparse`. "
            "Les exports TCX et l'export complet du compte n'en ont pas besoin."
        ) from exc

    try:
        fit = FitFile(str(chemin))
        sessions = list(fit.get_messages("session"))
        tours = list(fit.get_messages("lap"))
    except Exception as exc:  # noqa: BLE001 — fitparse remonte des erreurs variées
        raise ImportError_(f"FIT illisible : {exc}") from exc

    activites: list[Activity] = []
    for session in sessions:
        champs = {c.name: c.value for c in session}
        debut = champs.get("start_time")
        if not isinstance(debut, datetime):
            continue

        laps: list[Lap] = []
        for i, tour in enumerate(tours, start=1):
            t = {c.name: c.value for c in tour}
            distance = _f(t.get("total_distance")) or 0.0
            duree = _f(t.get("total_timer_time")) or _f(t.get("total_elapsed_time")) or 0.0
            laps.append(
                Lap(
                    index=i,
                    distance_m=distance,
                    duree_s=duree,
                    fc_moy=_i(t.get("avg_heart_rate")),
                    allure_s_km=(duree / (distance / 1000.0))
                    if distance > 0 and duree > 0
                    else None,
                    denivele_pos_m=_f(t.get("total_ascent")),
                )
            )

        activites.append(
            Activity(
                activity_id=_identifiant_local(debut),
                debut=debut,
                type=_type_depuis_sport(str(champs.get("sport") or "")),
                nom="",
                distance_m=_f(champs.get("total_distance")) or 0.0,
                duree_s=_f(champs.get("total_elapsed_time")) or 0.0,
                duree_mouvement_s=_f(champs.get("total_timer_time")),
                denivele_pos_m=_f(champs.get("total_ascent")),
                denivele_neg_m=_f(champs.get("total_descent")),
                fc_moy=_i(champs.get("avg_heart_rate")),
                fc_max=_i(champs.get("max_heart_rate")),
                cadence_moy=_i(champs.get("avg_running_cadence") or champs.get("avg_cadence")),
                puissance_moy=_i(champs.get("avg_power")),
                calories=_i(champs.get("total_calories")),
                laps=laps,
                raw={"source": "fit"},
            )
        )
    return activites


# ------------------------------------------------------------------ dispatch


def lire_fichier(chemin: Path) -> list[Activity]:
    """Lit un fichier unique, en choisissant le lecteur sur l'extension."""
    suffixe = chemin.suffix.lower()
    if suffixe == ".fit":
        return lire_fit(chemin)

    try:
        texte = chemin.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ImportError_(f"lecture impossible : {exc}") from exc

    if suffixe == ".json":
        return lire_json(texte)
    if suffixe == ".csv":
        return lire_csv(texte)
    if suffixe == ".tcx":
        return lire_tcx(texte)
    if suffixe == ".gpx":
        return lire_gpx(texte)
    raise ImportError_(f"format non reconnu : {suffixe or chemin.name}")


def _lire_zip(chemin: Path, resultat: ResultatImport) -> list[Activity]:
    """Parcourt une archive — l'export du compte, ou un lot de fichiers.

    L'export complet contient des centaines de fichiers dont la plupart ne nous
    concernent pas (sommeil, réglages, badges). On ne lit que les extensions
    connues et on ignore le reste en silence, sans le compter comme un échec.
    """
    activites: list[Activity] = []
    try:
        archive = zipfile.ZipFile(chemin)
    except zipfile.BadZipFile as exc:
        raise ImportError_(f"archive illisible : {exc}") from exc

    with archive:
        for membre in archive.namelist():
            nom = Path(membre)
            if membre.endswith("/") or nom.suffix.lower() not in SUFFIXES_CONNUS:
                continue
            if nom.suffix.lower() in (".zip", ".fit"):
                # Un zip imbriqué ou un FIT demandent un fichier sur disque ;
                # les extraire ici alourdirait sans servir le cas courant.
                resultat.ignorees.append(
                    (membre, "à extraire de l'archive avant import")
                )
                continue
            try:
                contenu = archive.read(membre).decode("utf-8", errors="replace")
            except (OSError, KeyError) as exc:
                resultat.ignorees.append((membre, str(exc)))
                continue
            try:
                if nom.suffix.lower() == ".json":
                    lues = lire_json(contenu)
                elif nom.suffix.lower() == ".csv":
                    lues = lire_csv(contenu)
                elif nom.suffix.lower() == ".tcx":
                    lues = lire_tcx(contenu)
                else:
                    lues = lire_gpx(contenu)
            except ImportError_ as exc:
                resultat.ignorees.append((membre, str(exc)))
                continue
            if lues:
                resultat.fichiers_lus += 1
                activites.extend(lues)
    return activites


def collecter(chemin: Path, resultat: ResultatImport) -> list[Activity]:
    """Rassemble les activités d'un fichier, d'une archive ou d'un dossier."""
    chemin = Path(chemin)
    if not chemin.exists():
        raise ImportError_(f"chemin introuvable : {chemin}")

    if chemin.is_dir():
        activites: list[Activity] = []
        for enfant in sorted(chemin.rglob("*")):
            if enfant.is_file() and enfant.suffix.lower() in SUFFIXES_CONNUS:
                activites.extend(collecter(enfant, resultat))
        return activites

    if chemin.suffix.lower() == ".zip":
        return _lire_zip(chemin, resultat)

    try:
        lues = lire_fichier(chemin)
    except ImportError_ as exc:
        resultat.ignorees.append((chemin.name, str(exc)))
        return []
    if lues:
        resultat.fichiers_lus += 1
    return lues


# ------------------------------------------------------------------ dédoublonnage


def _richesse(activity: Activity) -> tuple[int, int, int]:
    """De quoi départager deux exports de la même séance : le plus complet gagne."""
    return (
        len(activity.laps),
        sum(
            1
            for v in (
                activity.fc_moy,
                activity.fc_max,
                activity.cadence_moy,
                activity.puissance_moy,
                activity.vo2max,
                activity.training_effect_aerobie,
            )
            if v is not None
        ),
        1 if not activity.activity_id.startswith(("local-", "export-")) else 0,
    )


def dedoublonner(activites: list[Activity]) -> list[Activity]:
    """Ne garde qu'une activité par séance, la plus complète.

    Le même run peut arriver deux fois par deux chemins — le zip du compte et
    le TCX de la montre — sous deux identifiants différents. Les laisser entrer
    tous les deux doublerait la charge d'entraînement de cette journée.
    """
    retenues: list[Activity] = []
    for activity in sorted(activites, key=lambda a: a.debut):
        jumelle = next(
            (
                r
                for r in retenues
                if abs((r.debut - activity.debut).total_seconds())
                <= TOLERANCE_DOUBLON.total_seconds()
            ),
            None,
        )
        if jumelle is None:
            retenues.append(activity)
        elif _richesse(activity) > _richesse(jumelle):
            retenues[retenues.index(jumelle)] = activity
    return retenues


def _exclu(activity: Activity, motifs: tuple[str, ...]) -> bool:
    """Vrai si le type de l'activité correspond à l'un des motifs demandés.

    Le motif est comparé au type normalisé (`strength_training`) **et** au
    libellé d'origine (`Musculation`), parce qu'on écrit naturellement le mot
    qu'affiche Garmin, pas la clé interne. `--exclure musculation` doit écarter
    la musculation sans qu'il faille deviner sa traduction.
    """
    cible = activity.type.lower()
    libelle = str(activity.raw.get("type_libelle", "")).lower()
    for motif in motifs:
        m = motif.strip().lower()
        if not m:
            continue
        if m in cible or m in libelle or _type_csv(m) == cible:
            return True
    return False


def importer(
    chemin: Path,
    db: Database,
    verbose: bool = True,
    exclure: tuple[str, ...] = (),
) -> ResultatImport:
    """Lit `chemin` et enregistre ce qu'il contient, sans jamais doubler.

    `exclure` écarte des types d'activité (correspondance sur une portion du
    nom, par exemple `("musculation",)`). Utile quand une discipline est mal
    mesurée par la montre : la charge d'entraînement somme **toutes** les
    activités, pas seulement les courses, donc une séance douteuse pèse sur le
    CTL et l'ATL tant qu'elle est en base.
    """
    resultat = ResultatImport()
    activites = collecter(Path(chemin), resultat)

    valides: list[Activity] = []
    for activity in activites:
        if not activity.activity_id:
            continue
        if exclure and _exclu(activity, exclure):
            resultat.exclues += 1
            continue
        raison = _plausible(activity)
        if raison:
            resultat.ignorees.append((activity.activity_id, raison))
            continue
        valides.append(activity)

    # De la plus ancienne à la plus récente, comme la synchro : si l'import est
    # interrompu, la base ne garde jamais une activité plus récente qu'une
    # activité pas encore écrite.
    connues = {a.activity_id for a in db.activities()}
    debuts_connus = [a.debut for a in db.activities()]

    for activity in dedoublonner(valides):
        deja = activity.activity_id in connues
        if not deja and any(
            abs((d - activity.debut).total_seconds()) <= TOLERANCE_DOUBLON.total_seconds()
            for d in debuts_connus
        ):
            # Même séance déjà en base sous un autre identifiant (importée d'un
            # autre format, ou synchronisée en ligne autrefois).
            resultat.doublons += 1
            continue

        nouvelle = db.upsert_activity(activity)
        if nouvelle:
            resultat.nouvelles += 1
            debuts_connus.append(activity.debut)
        else:
            resultat.mises_a_jour += 1
        if verbose and nouvelle:
            print(
                f"  + {activity.debut:%Y-%m-%d %H:%M}  {activity.distance_km:5.1f} km  "
                f"{activity.type}"
            )
    return resultat
