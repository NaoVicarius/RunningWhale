"""Calculs d'entraînement : charge, forme, zones, allures, projections de perf.

Toutes les fonctions sont pures et testables : elles prennent des `Activity`
et un `Athlete`, et ne touchent ni au réseau ni à la base.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Athlete
from .models import Activity
from .composition import Composition, Tendance
from .composition import tendance as tendance_poids
from .wellness import Wellness

# Constantes de modélisation
COEF_RIEGEL = 1.06          # exposant de la formule de Riegel
JOURS_CTL = 42              # fenêtre de la charge chronique (forme de fond)
JOURS_ATL = 7               # fenêtre de la charge aiguë (fatigue)
ACWR_MIN_SAIN = 0.8         # sous ce ratio : désentraînement
ACWR_MAX_SAIN = 1.3         # au-dessus : sur-risque de blessure
ML_O2_PAR_KMH = 3.5         # coût énergétique approximatif de la course
# Intensité seuil par défaut, en fraction de la réserve cardiaque : le LTHR se
# situe typiquement vers 85 % de la réserve. Utilisée pour caler l'échelle du
# TRIMP quand `fc_seuil` n'est pas renseigné.
RATIO_RESERVE_SEUIL_DEFAUT = 0.85
# Profondeur d'historique minimale (en jours) pour publier un ACWR : en deçà,
# la moyenne chronique ne couvre qu'une fraction de sa fenêtre de 28 jours et
# le ratio couplé sature mécaniquement vers 4 (une seule séance suffit à
# afficher « risque de blessure élevé »). On exige les trois quarts de la
# fenêtre chronique.
JOURS_MIN_HISTORIQUE_ACWR = 21


# --------------------------------------------------------------------------
# Formatage
# --------------------------------------------------------------------------

def format_pace(s_km: float | None) -> str:
    """270.0 -> '4:30/km'."""
    if not s_km or s_km <= 0 or math.isinf(s_km):
        return "—"
    minutes, secondes = divmod(int(round(s_km)), 60)
    return f"{minutes}:{secondes:02d}/km"


def format_duration(secondes: float | None) -> str:
    """3725 -> '1h02:05' ; 305 -> '5:05'."""
    if secondes is None or secondes <= 0:
        return "—"
    total = int(round(secondes))
    heures, reste = divmod(total, 3600)
    minutes, sec = divmod(reste, 60)
    if heures:
        return f"{heures}h{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


# --------------------------------------------------------------------------
# Charge d'entraînement
# --------------------------------------------------------------------------

def charge_seance(activity: Activity, athlete: Athlete) -> float:
    """Charge d'une séance, sur une échelle où ≈100 = une heure au seuil.

    Trois méthodes, par ordre de préférence selon les données disponibles :
    1. TRIMP de Banister (nécessite FC moyenne, FC max et FC repos) ;
    2. charge basée sur l'allure rapportée à l'allure seuil ;
    3. repli sur la seule durée (60 points par heure).
    """
    duree_min = activity.duree_effective_s / 60.0
    if duree_min <= 0:
        return 0.0

    reserve = athlete.fc_reserve
    if activity.fc_moy and reserve and reserve > 0 and athlete.fc_repos:
        ratio_fc = (activity.fc_moy - athlete.fc_repos) / reserve
        ratio_fc = min(max(ratio_fc, 0.0), 1.0)
        # Un ratio nul signifie une FC moyenne au niveau (ou en dessous) de la
        # FC de repos : donnée aberrante (ceinture défaillante). On laisse alors
        # la main aux replis allure/durée plutôt que de rendre une charge nulle.
        if ratio_fc > 0:
            k = 1.67 if (athlete.sexe or "").upper().startswith("F") else 1.92
            # TRIMP exponentiel de Banister : durée_min × ratio × 0,64 × e^(k·ratio).
            # Sa valeur brute n'est PAS sur l'échelle « 100 = 1 h au seuil » :
            # une heure à ~85 % de réserve vaut ~167 chez un homme. Comme les
            # trois voies de calcul alimentent la même CTL, on normalise le
            # TRIMP pour qu'une heure à la FC seuil vaille 100, comme la voie
            # par l'allure (même principe que le hrTSS de TrainingPeaks).
            trimp = duree_min * ratio_fc * 0.64 * math.exp(k * ratio_fc)
            ratio_seuil = RATIO_RESERVE_SEUIL_DEFAUT
            if athlete.fc_seuil:
                ratio_seuil = (athlete.fc_seuil - athlete.fc_repos) / reserve
                ratio_seuil = min(max(ratio_seuil, 0.5), 0.95)
            trimp_seuil_1h = 60.0 * ratio_seuil * 0.64 * math.exp(k * ratio_seuil)
            return trimp / trimp_seuil_1h * 100.0

    allure = activity.allure_s_km
    if allure and athlete.allure_seuil_s_km:
        # Intensité = vitesse de la séance / vitesse seuil.
        intensite = athlete.allure_seuil_s_km / allure
        intensite = min(max(intensite, 0.0), 1.6)
        return (activity.duree_effective_s * intensite**2) / 3600.0 * 100.0

    return duree_min


def charge_journaliere(
    activities: list[Activity], athlete: Athlete
) -> dict[date, float]:
    """Somme des charges par jour."""
    par_jour: dict[date, float] = defaultdict(float)
    for a in activities:
        par_jour[a.debut.date()] += charge_seance(a, athlete)
    return dict(par_jour)


@dataclass
class EtatForme:
    """Photographie de la forme à une date donnée."""

    jour: date
    ctl: float       # charge chronique — condition physique de fond
    atl: float       # charge aiguë — fatigue récente
    tsb: float       # ctl - atl — fraîcheur disponible
    acwr: float | None  # ratio charge aiguë / chronique

    @property
    def lecture_tsb(self) -> str:
        if self.tsb > 15:
            return "très frais (possible désentraînement si prolongé)"
        if self.tsb > 5:
            return "frais, prêt à performer"
        if self.tsb > -10:
            return "équilibré, en construction"
        if self.tsb > -25:
            return "fatigue marquée, bloc de charge en cours"
        return "fatigue très élevée — surveiller la récupération"

    @property
    def lecture_acwr(self) -> str:
        if self.acwr is None:
            return "pas assez d'historique pour un ratio fiable"
        if self.acwr < ACWR_MIN_SAIN:
            return "charge en baisse — attention au désentraînement"
        if self.acwr <= ACWR_MAX_SAIN:
            return "progression de charge maîtrisée"
        if self.acwr <= 1.5:
            return "montée de charge rapide — vigilance"
        return "montée de charge très rapide — risque de blessure élevé"


def etat_forme(
    activities: list[Activity], athlete: Athlete, jour: date | None = None
) -> EtatForme:
    """Calcule CTL / ATL / TSB par moyennes mobiles exponentielles, et l'ACWR."""
    jour = jour or date.today()
    par_jour = charge_journaliere(activities, athlete)
    if not par_jour:
        return EtatForme(jour=jour, ctl=0.0, atl=0.0, tsb=0.0, acwr=None)

    debut = min(par_jour)
    ctl = atl = 0.0
    # Lissage exponentiel à constante de temps de N jours, convention
    # Banister / TrainingPeaks : alpha = 1 − e^(−1/N). L'ancienne convention
    # financière 2/(N+1) divisait par deux la constante de temps effective
    # (une « CTL 42 jours » qui réagissait comme une 21 jours), ce qui
    # décalait le TSB par rapport aux seuils d'interprétation de lecture_tsb.
    alpha_ctl = 1.0 - math.exp(-1.0 / JOURS_CTL)
    alpha_atl = 1.0 - math.exp(-1.0 / JOURS_ATL)

    courant = debut
    while courant <= jour:
        charge = par_jour.get(courant, 0.0)
        ctl = ctl + alpha_ctl * (charge - ctl)
        atl = atl + alpha_atl * (charge - atl)
        courant += timedelta(days=1)

    # ACWR « couplé » : la fenêtre aiguë (7 j) est incluse dans la fenêtre
    # chronique (28 j), méthode originale de Hulin/Gabbett. La littérature
    # récente discute une variante découplée (chronique = jours 8 à 28) ;
    # on garde la version couplée, majoritaire, à laquelle se réfèrent les
    # seuils 0,8–1,3.
    aigue = sum(c for d, c in par_jour.items() if jour - timedelta(days=7) < d <= jour)
    chronique = sum(
        c for d, c in par_jour.items() if jour - timedelta(days=28) < d <= jour
    ) / 4.0
    # Sans historique chronique, le ratio serait numériquement instable ; et
    # avec un historique trop court (moins de JOURS_MIN_HISTORIQUE_ACWR jours),
    # il saturerait mécaniquement vers 4 dès la première séance.
    profondeur_j = (jour - debut).days
    acwr = (
        round(aigue / chronique, 2)
        if chronique > 5 and profondeur_j >= JOURS_MIN_HISTORIQUE_ACWR
        else None
    )

    return EtatForme(jour=jour, ctl=round(ctl, 1), atl=round(atl, 1),
                     tsb=round(ctl - atl, 1), acwr=acwr)


# --------------------------------------------------------------------------
# Volumes hebdomadaires
# --------------------------------------------------------------------------

@dataclass
class Semaine:
    debut: date            # lundi
    distance_km: float
    duree_s: float
    denivele_m: float
    seances: int
    charge: float

    @property
    def libelle(self) -> str:
        return f"{self.debut:%d/%m}"


def semaines(
    activities: list[Activity], athlete: Athlete, nombre: int = 8,
    jusqu_au: date | None = None,
) -> list[Semaine]:
    """Résumé des `nombre` dernières semaines, de la plus ancienne à la plus récente."""
    fin = jusqu_au or date.today()
    lundi_courant = fin - timedelta(days=fin.weekday())
    debuts = [lundi_courant - timedelta(weeks=i) for i in range(nombre - 1, -1, -1)]

    resultat: list[Semaine] = []
    for debut in debuts:
        fin_semaine = debut + timedelta(days=7)
        lot = [a for a in activities if debut <= a.debut.date() < fin_semaine]
        resultat.append(
            Semaine(
                debut=debut,
                distance_km=round(sum(a.distance_km for a in lot), 1),
                duree_s=sum(a.duree_effective_s for a in lot),
                denivele_m=round(sum(a.denivele_pos_m or 0 for a in lot)),
                seances=len(lot),
                charge=round(sum(charge_seance(a, athlete) for a in lot), 1),
            )
        )
    return resultat


# --------------------------------------------------------------------------
# Zones et répartition d'intensité
# --------------------------------------------------------------------------

ZONES_FC = [
    ("Z1 — récupération", 0.00, 0.60),
    ("Z2 — endurance fondamentale", 0.60, 0.70),
    ("Z3 — tempo", 0.70, 0.80),
    ("Z4 — seuil", 0.80, 0.90),
    ("Z5 — VMA", 0.90, 1.01),
]


def zone_de_fc(fc: int | None, athlete: Athlete) -> str | None:
    """Zone d'une FC donnée, en % de réserve cardiaque (Karvonen) si possible.

    Les bornes de ZONES_FC suivent, pour chaque voie, le découpage classique de
    sa propre convention : 60/70/80/90 % de la réserve (Karvonen) comme
    60/70/80/90 % de la FC max (schéma « textbook » à 5 zones, défaut Garmin).
    Les deux conventions ne décrivent pas exactement la même intensité
    physiologique (x % de FC max est plus facile que x % de réserve) : le repli
    en % de FC max, utilisé quand `fc_repos` manque, classe donc un même effort
    un peu plus haut. C'est l'imprécision assumée du repli, pas un bug.
    """
    if not fc or not athlete.fc_max:
        return None
    if athlete.fc_repos and athlete.fc_reserve:
        ratio = (fc - athlete.fc_repos) / athlete.fc_reserve
    else:
        ratio = fc / athlete.fc_max
    # Une FC sous la FC de repos (donnée aberrante) donnerait un ratio négatif
    # qui ne matcherait aucune zone et retomberait sur… Z5 via le repli final.
    # On la range en Z1.
    ratio = max(ratio, 0.0)
    for nom, bas, haut in ZONES_FC:
        if bas <= ratio < haut:
            return nom
    return ZONES_FC[-1][0]


def bornes_zones_fc(athlete: Athlete) -> dict[str, tuple[int, int]]:
    """Les cinq zones traduites en battements par minute.

    Le découpage en pourcentages ne se pilote pas sur le terrain : personne ne
    calcule 70 % de sa réserve cardiaque en courant. Une fourchette en bpm, si.
    C'est la forme sous laquelle une consigne de séance devient exécutable.
    """
    if not athlete.fc_max:
        return {}

    def vers_bpm(ratio: float) -> int:
        if athlete.fc_repos and athlete.fc_reserve:
            return int(round(athlete.fc_repos + ratio * athlete.fc_reserve))
        return int(round(ratio * athlete.fc_max))

    bornes: dict[str, tuple[int, int]] = {}
    for nom, bas, haut in ZONES_FC:
        bornes[nom] = (vers_bpm(bas), min(vers_bpm(min(haut, 1.0)), athlete.fc_max))
    return bornes


@dataclass
class RepartitionIntensite:
    """Répartition du temps entre facile, modéré et dur (modèle polarisé)."""

    facile_pct: float      # Z1-Z2
    modere_pct: float      # Z3
    dur_pct: float         # Z4-Z5
    base: str              # "tours" ou "séances" — précision de la mesure
    temps_total_s: float

    @property
    def lecture(self) -> str:
        if self.temps_total_s <= 0:
            return "pas de données de fréquence cardiaque exploitables"
        if self.facile_pct >= 75 and self.dur_pct >= 10:
            return "répartition polarisée conforme aux recommandations"
        if self.modere_pct > 35:
            return "trop de temps en zone intermédiaire (« zone grise »)"
        if self.facile_pct < 70:
            return "pas assez d'endurance fondamentale"
        return "beaucoup de facile, peu de qualité — marge pour ajouter de l'intensité"


def repartition_intensite(
    activities: list[Activity], athlete: Athlete
) -> RepartitionIntensite:
    """Temps passé par bloc d'intensité, à partir des tours quand ils existent."""
    seaux = {"facile": 0.0, "modere": 0.0, "dur": 0.0}
    base = "séances"
    total = 0.0

    for a in activities:
        # Les tours donnent une image bien plus juste qu'une FC moyenne
        # de séance, qui lisse un fractionné en une intensité "modérée".
        segments = (
            [(lap.fc_moy, lap.duree_s) for lap in a.laps if lap.fc_moy and lap.duree_s]
            if a.laps
            else []
        )
        if segments:
            base = "tours"
        elif a.fc_moy:
            segments = [(a.fc_moy, a.duree_effective_s)]
        else:
            continue

        for fc, duree in segments:
            zone = zone_de_fc(fc, athlete)
            if zone is None:
                continue
            total += duree
            if zone.startswith(("Z1", "Z2")):
                seaux["facile"] += duree
            elif zone.startswith("Z3"):
                seaux["modere"] += duree
            else:
                seaux["dur"] += duree

    if total <= 0:
        return RepartitionIntensite(0.0, 0.0, 0.0, base, 0.0)

    return RepartitionIntensite(
        facile_pct=round(seaux["facile"] / total * 100, 1),
        modere_pct=round(seaux["modere"] / total * 100, 1),
        dur_pct=round(seaux["dur"] / total * 100, 1),
        base=base,
        temps_total_s=total,
    )


# --------------------------------------------------------------------------
# Performances et projections
# --------------------------------------------------------------------------

DISTANCES_REFERENCE = [
    ("5 km", 5.0),
    ("10 km", 10.0),
    ("Semi-marathon", 21.0975),
    ("Marathon", 42.195),
]
TOLERANCE_DISTANCE = 0.04  # ±4 % pour rattacher une sortie à une distance de référence


@dataclass
class Record:
    libelle: str
    distance_km: float
    temps_s: float
    quand: date
    activity_id: str

    @property
    def allure_s_km(self) -> float:
        return self.temps_s / self.distance_km


def records(activities: list[Activity]) -> list[Record]:
    """Meilleur temps par distance de référence, sur les activités enregistrées.

    On ne détecte que les sorties dont la distance totale correspond à une
    distance de référence : pas d'extraction de « meilleur 10 km à l'intérieur
    d'une sortie longue », qui exigerait les séries GPS complètes.
    """
    meilleurs: dict[str, Record] = {}
    for a in activities:
        if not a.est_course or a.duree_effective_s <= 0:
            continue
        for libelle, distance in DISTANCES_REFERENCE:
            ecart = abs(a.distance_km - distance) / distance
            if ecart > TOLERANCE_DISTANCE:
                continue
            # Temps ramené à la distance exacte, pour comparer équitablement
            # une sortie de 5,1 km et une de 4,9 km.
            temps_normalise = a.duree_effective_s * (distance / a.distance_km)
            actuel = meilleurs.get(libelle)
            if actuel is None or temps_normalise < actuel.temps_s:
                meilleurs[libelle] = Record(
                    libelle=libelle,
                    distance_km=distance,
                    temps_s=temps_normalise,
                    quand=a.debut.date(),
                    activity_id=a.activity_id,
                )
    return [meilleurs[lib] for lib, _ in DISTANCES_REFERENCE if lib in meilleurs]


def riegel(temps_s: float, distance_km: float, cible_km: float) -> float:
    """Projette un temps sur une autre distance (formule de Riegel)."""
    return temps_s * (cible_km / distance_km) ** COEF_RIEGEL


# Au-delà de ce rapport entre distance cible et distance de référence (dans un
# sens ou dans l'autre), la projection de Riegel sort de son domaine de
# validité empirique (efforts d'environ 3,5 min à 4 h, rapports de distance
# modérés) et devient notoirement optimiste — un marathon prédit depuis un
# 5 km suppose une endurance spécifique que le 5 km ne démontre pas.
FACTEUR_EXTRAPOLATION_RIEGEL = 4.0


@dataclass
class Projection:
    libelle: str
    distance_km: float
    temps_s: float
    source: str
    distance_reference_km: float | None = None

    @property
    def allure_s_km(self) -> float:
        return self.temps_s / self.distance_km

    @property
    def extrapolation_lointaine(self) -> bool:
        """Vrai si la cible est très éloignée de la distance de référence."""
        if not self.distance_reference_km or self.distance_reference_km <= 0:
            return False
        rapport = self.distance_km / self.distance_reference_km
        return rapport > FACTEUR_EXTRAPOLATION_RIEGEL or rapport < 1.0 / FACTEUR_EXTRAPOLATION_RIEGEL


DISTANCE_ETALON_KM = 10.0  # distance de comparaison des performances entre elles


def meilleure_reference(refs: list[Record]) -> Record | None:
    """Choisit la performance la plus aboutie parmi les records disponibles.

    On ne peut pas simplement prendre la plus longue : une sortie longue tranquille
    de 21 km n'est pas une performance et donnerait des projections plus lentes que
    le temps déjà réalisé sur 10 km. On ramène donc chaque référence à un temps
    équivalent sur une distance étalon, et on retient la plus rapide.
    """
    if not refs:
        return None
    return min(
        refs,
        key=lambda r: riegel(r.temps_s, r.distance_km, DISTANCE_ETALON_KM),
    )


def projections(activities: list[Activity], athlete: Athlete) -> list[Projection]:
    """Temps projetés sur les distances de référence, depuis la meilleure perf récente."""
    recents = [a for a in activities if a.debut.date() >= date.today() - timedelta(days=120)]
    refs = records(recents) or records(activities)
    base = meilleure_reference(refs)
    if base is None:
        return []

    return [
        Projection(
            libelle=libelle,
            distance_km=distance,
            temps_s=riegel(base.temps_s, base.distance_km, distance),
            source=f"{base.libelle} du {base.quand:%d/%m/%Y}",
            distance_reference_km=base.distance_km,
        )
        for libelle, distance in DISTANCES_REFERENCE
    ]


# ---------------------------------------------------------------------------
# Version des calculs d'estimation
# ---------------------------------------------------------------------------
# Chaque valeur estimée est servie avec ce numéro : un rapport ou un plan
# archivé reste traçable quand une formule change. Historique :
#   v1 — formules initiales : VMA centrale (VO2max/3,5 ou meilleur 5 km à
#        92 % de VMA), servie comme un chiffre unique.
#   v2 (2026-08-18) — retour utilisateur : aucun algorithme ne donne une VMA,
#        un VO2max ou des zones fiables face à un test en labo. Chaque
#        estimation porte désormais sa source et sa fourchette d'incertitude,
#        et les prescriptions se calent sur la borne basse : au pire on
#        s'entraîne un peu trop doucement, jamais trop fort.
VERSION_CALCULS = "v2"


@dataclass
class EstimationVMA:
    """Une VMA estimée, avec ce qu'il faut pour ne pas la prendre pour une mesure."""

    valeur: float                    # borne basse — celle qui sert aux prescriptions
    fourchette: tuple[float, float]  # plage plausible en km/h
    source: str                      # d'où vient l'estimation, en toutes lettres
    version: str = VERSION_CALCULS

    @property
    def mesuree(self) -> bool:
        """Vrai pour une valeur déclarée par l'athlète, sans incertitude ajoutée."""
        return self.fourchette[0] == self.fourchette[1]


# Incertitude retenue pour un VO2max de montre : les études de validation
# donnent ±5 à 10 % selon les modèles, avec un biais plutôt optimiste chez les
# coureurs peu entraînés — d'où une fourchette asymétrique autour de la centrale.
_VO2_MARGE_BASSE = 0.90
_VO2_MARGE_HAUTE = 1.05

# Un 5 km se court à ≈92 % de VMA chez un coureur entraîné ; un débutant tient
# un pourcentage plus faible, donc la centrale sous-estime déjà sa VMA. La
# borne basse est la centrale elle-même ; l'incertitude est toute vers le haut.
_5K_MARGE_HAUTE = 1.10


def vma_avec_provenance(
    activities: list[Activity], athlete: Athlete
) -> EstimationVMA | None:
    """VMA en km/h avec source, fourchette et version du calcul.

    Trois sources en cascade : profil déclaré (aucune incertitude ajoutée —
    c'est la valeur de l'athlète), sinon VO2max de la montre, sinon meilleur
    temps ramené à un 5 km. Pour les deux sources indirectes, `valeur` est la
    borne basse de la fourchette : c'est elle qui alimente les allures, pour
    que l'erreur éventuelle rende l'entraînement trop doux, pas trop dur.
    """
    if athlete.vma_kmh:
        v = round(athlete.vma_kmh, 1)
        return EstimationVMA(
            valeur=v, fourchette=(v, v), source="déclarée dans le profil (vma_kmh)"
        )

    vo2 = next((a.vo2max for a in activities if a.vo2max), None)
    if vo2:
        centrale = vo2 / ML_O2_PAR_KMH
        bas = round(centrale * _VO2_MARGE_BASSE, 1)
        haut = round(centrale * _VO2_MARGE_HAUTE, 1)
        return EstimationVMA(
            valeur=bas,
            fourchette=(bas, haut),
            source=f"VO2max de la montre ({vo2:.0f} ml/kg/min)",
        )

    base = meilleure_reference(records(activities))
    if base:
        # On ramène la meilleure performance à un 5 km équivalent (Riegel),
        # couru à environ 92 % de la VMA.
        temps_5k = riegel(base.temps_s, base.distance_km, 5.0)
        vitesse_5k = 5.0 / (temps_5k / 3600.0)
        centrale = vitesse_5k / 0.92
        bas = round(centrale, 1)
        return EstimationVMA(
            valeur=bas,
            fourchette=(bas, round(centrale * _5K_MARGE_HAUTE, 1)),
            source=f"{base.libelle} du {base.quand:%d/%m/%Y}, ramené à un 5 km (Riegel)",
        )
    return None


def vma_estimee(activities: list[Activity], athlete: Athlete) -> float | None:
    """VMA en km/h : la borne basse de `vma_avec_provenance`, ou None."""
    est = vma_avec_provenance(activities, athlete)
    return est.valeur if est else None


# Proportion jambe / taille, moyenne anthropométrique usuelle.
RATIO_JAMBE_TAILLE = 0.53
TAILLE_PAR_DEFAUT_CM = 175.0
G = 9.81


def vitesse_transition_marche_course(taille_cm: float | None) -> float:
    """Vitesse à partir de laquelle courir devient plus économique que marcher.

    Transition donnée par un nombre de Froude de 0,5 : `v = √(0,5·g·L)`, où `L`
    est la longueur de jambe. Elle dépend donc de la taille — un athlète d'1m95
    marche vite bien plus longtemps qu'un athlète d'1m65, et une allure « lente »
    prescrite sans en tenir compte lui demande de marcher, pas de courir.
    """
    taille = taille_cm or TAILLE_PAR_DEFAUT_CM
    longueur_jambe_m = taille / 100.0 * RATIO_JAMBE_TAILLE
    return math.sqrt(0.5 * G * longueur_jambe_m) * 3.6


def allures_entrainement(vma: float | None, athlete: Athlete) -> dict[str, str]:
    """Fourchettes d'allures utilisables en séance, dérivées de la VMA.

    Les pourcentages de VMA sont calibrés sur des coureurs entraînés (VMA de
    15 à 20 km/h). Appliqués tels quels à une VMA basse, ils produisent des
    allures d'endurance **sous la vitesse de marche** : à 9 km/h de VMA, 65 %
    valent 5,9 km/h, que personne ne court — on la marche. Les allures
    concernées sont donc signalées plutôt que servies telles quelles.
    """
    if not vma or vma <= 0:
        return {}

    transition = vitesse_transition_marche_course(athlete.taille_cm)

    def allure(pct: float) -> float:
        return 3600.0 / (vma * pct)

    def afficher(pct: float) -> str:
        return format_pace(allure(pct))

    def afficher_plage(bas: float, haut: float) -> str:
        texte = f"{format_pace(allure(haut))} – {format_pace(allure(bas))}"
        # La borne haute en allure est la vitesse la plus lente de la plage.
        if vma * bas < transition:
            texte += " ⚠️ sous ta transition marche/course"
        return texte

    allures = {
        "Endurance fondamentale (65-70 % VMA)": afficher_plage(0.65, 0.70),
        "Endurance active (75 % VMA)": afficher(0.75),
        "Allure marathon (~80 % VMA)": afficher(0.80),
        "Allure semi (~85 % VMA)": afficher(0.85),
        "Seuil / allure 10 km (~90 % VMA)": afficher(0.90),
        "Allure 5 km (~92 % VMA)": afficher(0.92),
        "VMA courte (100 % VMA)": afficher(1.00),
    }
    for libelle, pct in (
        ("Endurance active (75 % VMA)", 0.75),
        ("Allure marathon (~80 % VMA)", 0.80),
    ):
        if vma * pct < transition:
            allures[libelle] += " ⚠️ sous ta transition marche/course"
    return allures


def allures_sous_la_marche(vma: float | None, athlete: Athlete) -> str | None:
    """Message d'alerte quand les allures faciles calculées sont immarchables.

    Renvoie `None` tant que l'endurance fondamentale reste courable. Sinon, dit
    quoi faire à la place : se caler sur la fréquence cardiaque, qui reste
    valide quelle que soit la VMA, et alterner course et marche.
    """
    if not vma or vma <= 0:
        return None
    transition = vitesse_transition_marche_course(athlete.taille_cm)
    if vma * 0.70 >= transition:
        return None

    plafond_fc = None
    if athlete.fc_max and athlete.fc_repos:
        plafond_fc = int(athlete.fc_repos + 0.70 * (athlete.fc_max - athlete.fc_repos))

    taille = athlete.taille_cm or TAILLE_PAR_DEFAUT_CM
    message = (
        f"Les allures faciles calculées ({format_pace(3600.0 / (vma * 0.70))} et "
        f"plus lent) passent sous ta transition marche/course "
        f"(~{transition:.1f} km/h pour {taille:.0f} cm) : à cette vitesse, "
        "marcher devient plus naturel que courir. On peut courir plus lentement, "
        "mais la foulée s'y déforme et le geste coûte plus cher qu'il ne rapporte. "
        "Les pourcentages de VMA sont calibrés sur des coureurs entraînés et ne "
        "tiennent pas à VMA basse. "
    )
    if plafond_fc:
        message += (
            f"Pilote tes sorties faciles à la fréquence cardiaque — reste sous "
            f"{plafond_fc} bpm — et alterne course et marche si elle monte trop."
        )
    else:
        message += (
            "Pilote tes sorties faciles à la sensation (conversation possible) "
            "et alterne course et marche si l'effort monte."
        )
    return message


# --------------------------------------------------------------------------
# Analyse d'une séance isolée
# --------------------------------------------------------------------------

@dataclass
class AnalyseSeance:
    activity: Activity
    charge: float
    zone_dominante: str | None
    decouplage_pct: float | None   # dérive cardiaque entre 1re et 2e moitié
    negative_split: bool | None
    ecart_allure_seuil: str | None
    laps_resume: list[str] = field(default_factory=list)

    @property
    def lecture_decouplage(self) -> str:
        if self.decouplage_pct is None:
            return "dérive cardiaque non mesurable (données de tours ou FC absentes)"
        if self.decouplage_pct < 5:
            return "dérive cardiaque faible — bonne endurance aérobie sur cet effort"
        if self.decouplage_pct < 10:
            return "dérive cardiaque modérée — allure un peu ambitieuse ou chaleur"
        return "dérive cardiaque forte — départ trop rapide, fatigue ou déshydratation"


def analyse_seance(activity: Activity, athlete: Athlete) -> AnalyseSeance:
    """Décortique une séance : charge, intensité, dérive cardiaque, gestion d'allure."""
    decouplage = _decouplage(activity)
    negative = _negative_split(activity)

    ecart = None
    if activity.allure_s_km and athlete.allure_seuil_s_km:
        delta = activity.allure_s_km - athlete.allure_seuil_s_km
        signe = "plus lent" if delta > 0 else "plus rapide"
        ecart = f"{abs(int(delta))} s/km {signe} que l'allure seuil"

    resume_laps = [
        f"T{lap.index} : {lap.distance_m / 1000:.2f} km en {format_duration(lap.duree_s)}"
        f" ({format_pace(lap.allure_s_km)}"
        + (f", {lap.fc_moy} bpm" if lap.fc_moy else "")
        + ")"
        for lap in activity.laps[:20]
    ]

    return AnalyseSeance(
        activity=activity,
        charge=round(charge_seance(activity, athlete), 1),
        zone_dominante=zone_de_fc(activity.fc_moy, athlete),
        decouplage_pct=decouplage,
        negative_split=negative,
        ecart_allure_seuil=ecart,
        laps_resume=resume_laps,
    )


def _coupe_a_mi_temps(laps: list) -> int:
    """Indice de coupe des tours au plus près de la moitié du temps total.

    Le Pa:HR standard (et le negative split) comparent la 1re et la 2e moitié
    TEMPORELLE de l'effort. Couper à mi-nombre-de-tours biaise dès que les
    tours sont inégaux — avec un échauffement de 30 min suivi de fractionnés
    de 90 s, la « première moitié » couvrirait 80 % du temps. On coupe donc à
    la frontière de tour la plus proche de la moitié de la durée totale,
    chaque moitié restant non vide.
    """
    total_s = sum(l.duree_s for l in laps)
    milieu, cumul, meilleur_ecart = 1, 0.0, float("inf")
    for i, lap in enumerate(laps[:-1], start=1):
        cumul += lap.duree_s
        ecart = abs(cumul - total_s / 2)
        if ecart < meilleur_ecart:
            meilleur_ecart, milieu = ecart, i
    return milieu


def _decouplage(activity: Activity) -> float | None:
    """Perte d'efficacité (vitesse/FC) entre la première et la seconde moitié.

    Note : sur une séance à allure très variable (fractionné), la métrique
    reste par nature peu significative — elle est pensée pour les efforts
    réguliers.
    """
    laps = [l for l in activity.laps if l.fc_moy and l.allure_s_km and l.duree_s > 0]
    if len(laps) < 4:
        return None

    milieu = _coupe_a_mi_temps(laps)
    def efficacite(lot: list) -> float | None:
        duree = sum(l.duree_s for l in lot)
        distance = sum(l.distance_m for l in lot)
        if duree <= 0 or distance <= 0:
            return None
        fc_moy = sum(l.fc_moy * l.duree_s for l in lot) / duree
        if fc_moy <= 0:
            return None
        vitesse = distance / duree  # m/s
        return vitesse / fc_moy

    ef1, ef2 = efficacite(laps[:milieu]), efficacite(laps[milieu:])
    if not ef1 or not ef2:
        return None
    return round((ef1 - ef2) / ef1 * 100, 1)


def _negative_split(activity: Activity) -> bool | None:
    """Vrai si la seconde moitié a été courue plus vite que la première."""
    laps = [l for l in activity.laps if l.allure_s_km and l.distance_m > 0]
    if len(laps) < 2:
        return None
    milieu = _coupe_a_mi_temps(laps)
    def allure_moyenne(lot: list) -> float:
        return sum(l.duree_s for l in lot) / (sum(l.distance_m for l in lot) / 1000.0)

    return allure_moyenne(laps[milieu:]) < allure_moyenne(laps[:milieu])


# --------------------------------------------------------------------------
# Récupération
# --------------------------------------------------------------------------

CIBLE_SOMMEIL_H = 8.0        # référence de besoin de sommeil pour un athlète d'endurance
SEUIL_DETTE_SOMMEIL_H = 5.0  # dette cumulée sur 7 jours au-delà de laquelle on alerte
HAUSSE_FC_REPOS_ALERTE = 3   # bpm d'écart entre la semaine et le mois
SEUIL_READINESS_BAS = 40


@dataclass
class Recuperation:
    """Lecture des signaux de récupération sur les derniers jours."""

    dernier: Wellness | None
    jours_couverts: int
    sommeil_moyen_h: float | None
    dette_sommeil_h: float | None
    vfc_moyenne_7j: float | None
    vfc_jours_sous_baseline: int
    fc_repos_7j: float | None
    fc_repos_28j: float | None
    alertes: list[str] = field(default_factory=list)

    @property
    def disponible(self) -> bool:
        return self.jours_couverts > 0

    @property
    def derive_fc_repos(self) -> float | None:
        """Écart entre la FC de repos de la semaine et celle du mois."""
        if self.fc_repos_7j is None or self.fc_repos_28j is None:
            return None
        return round(self.fc_repos_7j - self.fc_repos_28j, 1)

    @property
    def lecture(self) -> str:
        if not self.disponible:
            return "aucune donnée de récupération synchronisée"
        if self.alertes:
            return "signaux de récupération dégradés — voir les alertes"
        return "récupération dans la norme"


def recuperation(jours: list[Wellness], reference: date | None = None) -> Recuperation:
    """Agrège les signaux de récupération et lève les alertes qui comptent."""
    reference = reference or date.today()
    utiles = [w for w in jours if not w.est_vide]
    if not utiles:
        return Recuperation(None, 0, None, None, None, 0, None, None)

    utiles.sort(key=lambda w: w.jour, reverse=True)
    sur_7j = [w for w in utiles if (reference - w.jour).days < 7]
    sur_28j = [w for w in utiles if (reference - w.jour).days < 28]

    def moyenne(valeurs: list[float]) -> float | None:
        return round(sum(valeurs) / len(valeurs), 1) if valeurs else None

    sommeils = [w.sommeil_s / 3600 for w in sur_7j if w.sommeil_s]
    sommeil_moyen = moyenne(sommeils)
    dette = (
        round(sum(max(CIBLE_SOMMEIL_H - h, 0) for h in sommeils), 1) if sommeils else None
    )

    vfc_7j = moyenne([w.vfc_ms for w in sur_7j if w.vfc_ms])
    # On compte les jours consécutifs récents sous la fourchette habituelle :
    # c'est la répétition qui fait signal, pas une nuit isolée.
    consecutifs = 0
    for w in utiles:
        if w.vfc_sous_baseline is True:
            consecutifs += 1
        elif w.vfc_sous_baseline is False:
            break

    fc_7j = moyenne([float(w.fc_repos) for w in sur_7j if w.fc_repos])
    fc_28j = moyenne([float(w.fc_repos) for w in sur_28j if w.fc_repos])

    recup = Recuperation(
        dernier=utiles[0],
        jours_couverts=len(utiles),
        sommeil_moyen_h=sommeil_moyen,
        dette_sommeil_h=dette,
        vfc_moyenne_7j=vfc_7j,
        vfc_jours_sous_baseline=consecutifs,
        fc_repos_7j=fc_7j,
        fc_repos_28j=fc_28j,
    )
    recup.alertes = _alertes_recuperation(recup)
    return recup


def _alertes_recuperation(r: Recuperation) -> list[str]:
    alertes: list[str] = []

    if r.vfc_jours_sous_baseline >= 2:
        alertes.append(
            f"VFC sous la fourchette habituelle depuis {r.vfc_jours_sous_baseline} jours"
            " — signe de fatigue ou de stress non digéré"
        )

    derive = r.derive_fc_repos
    if derive is not None and derive >= HAUSSE_FC_REPOS_ALERTE:
        alertes.append(
            f"FC de repos en hausse de {derive:.0f} bpm sur la semaine par rapport"
            " au mois — récupération incomplète, début d'infection ou charge trop forte"
        )

    if r.dette_sommeil_h is not None and r.dette_sommeil_h >= SEUIL_DETTE_SOMMEIL_H:
        alertes.append(
            f"dette de sommeil de {r.dette_sommeil_h:.0f} h cumulée sur la semaine"
            f" (cible {CIBLE_SOMMEIL_H:.0f} h par nuit)"
        )

    if r.dernier and r.dernier.readiness_score is not None:
        if r.dernier.readiness_score < SEUIL_READINESS_BAS:
            alertes.append(
                f"readiness Garmin à {r.dernier.readiness_score}/100 ce matin"
                f" ({r.dernier.readiness_niveau or 'niveau bas'})"
            )

    return alertes


# --------------------------------------------------------------------------
# Synthèse globale
# --------------------------------------------------------------------------

@dataclass
class Bilan:
    """Toutes les métriques agrégées, prêtes à être affichées ou envoyées au coach."""

    genere_le: datetime
    nb_activites: int
    forme: EtatForme
    semaines: list[Semaine]
    repartition: RepartitionIntensite
    records: list[Record]
    projections: list[Projection]
    vma_kmh: float | None
    allures: dict[str, str]
    derniere_seance: AnalyseSeance | None
    recuperation: Recuperation
    composition: Composition | None = None
    tendance_poids: Tendance | None = None
    alerte_allures: str | None = None
    zones_fc: dict[str, tuple[int, int]] = field(default_factory=dict)
    vma_provenance: EstimationVMA | None = None

    @property
    def semaines_actives_recentes(self) -> int:
        """Semaines avec au moins une séance parmi les 4 dernières (fenêtre de l'ACWR)."""
        return sum(1 for s in self.semaines[-4:] if s.seances > 0)

    @property
    def historique_recent_court(self) -> bool:
        """Vrai quand la fenêtre chronique (28 jours) est trop creuse.

        Un athlète tout neuf ou en reprise n'a de la charge que sur une ou deux
        semaines : la charge chronique est mécaniquement écrasée et l'ACWR tend
        vers sa borne maximale (4,0) quelle que soit la prudence réelle de la
        reprise. Ces indicateurs doivent alors être présentés comme indicatifs,
        pas comme des mesures.
        """
        return self.semaines_actives_recentes < 3


def bilan(
    activities: list[Activity],
    athlete: Athlete,
    nb_semaines: int = 8,
    wellness: list[Wellness] | None = None,
    compositions: list[Composition] | None = None,
) -> Bilan:
    """Assemble l'ensemble des indicateurs à partir de l'historique de courses."""
    courses = [a for a in activities if a.est_course]
    provenance = vma_avec_provenance(courses, athlete)
    vma = provenance.valeur if provenance else None
    derniere = courses[0] if courses else None

    return Bilan(
        recuperation=recuperation(wellness or []),
        genere_le=datetime.now(),
        nb_activites=len(courses),
        forme=etat_forme(activities, athlete),
        semaines=semaines(courses, athlete, nombre=nb_semaines),
        repartition=repartition_intensite(
            [a for a in courses if a.debut.date() >= date.today() - timedelta(days=28)],
            athlete,
        ),
        records=records(courses),
        projections=projections(courses, athlete),
        vma_kmh=vma,
        vma_provenance=provenance,
        allures=allures_entrainement(vma, athlete),
        alerte_allures=allures_sous_la_marche(vma, athlete),
        zones_fc=bornes_zones_fc(athlete),
        derniere_seance=analyse_seance(derniere, athlete) if derniere else None,
        composition=(sorted(compositions, key=lambda c: c.jour)[-1] if compositions else None),
        tendance_poids=tendance_poids(compositions or []),
    )
