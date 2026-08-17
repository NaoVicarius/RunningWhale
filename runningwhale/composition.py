"""Composition corporelle : poids et ce qu'il y a dedans, dans le temps.

Une balance connectée (Renpho, Withings, Garmin Index) mesure bien plus qu'un
poids. Pour un coureur, deux choses comptent vraiment :

* la **tendance** du poids, pas la mesure du jour — l'hydratation, le repas et
  l'heure de la pesée font varier le chiffre de plus d'un kilo d'un matin à
  l'autre, ce qui rend une mesure isolée presque muette ;
* la **part de masse maigre**, parce qu'une perte de poids qui vient du muscle
  n'améliore pas l'économie de course, elle la dégrade.

Les pourcentages de graisse d'une balance à impédance sont indicatifs : la
mesure dépend de l'hydratation et diffère de plusieurs points d'un appareil à
l'autre. Leur variation dans le temps, à balance constante, vaut mieux que leur
valeur absolue — c'est ainsi que le coach les présente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class Composition:
    """Une pesée. Tout est optionnel sauf la date et le poids."""

    jour: date
    poids_kg: float

    imc: float | None = None
    graisse_pct: float | None = None
    muscle_squelettique_pct: float | None = None
    masse_hors_graisse_kg: float | None = None
    gras_sous_cutane_pct: float | None = None
    graisse_viscerale: float | None = None
    eau_pct: float | None = None
    masse_musculaire_kg: float | None = None
    masse_osseuse_kg: float | None = None
    proteines_pct: float | None = None
    metabolisme_base_kcal: int | None = None
    age_metabolique: int | None = None
    source: str = ""

    @property
    def masse_grasse_kg(self) -> float | None:
        if self.graisse_pct is None:
            return None
        return self.poids_kg * self.graisse_pct / 100.0

    @property
    def masse_maigre_kg(self) -> float | None:
        """Ce qui n'est pas de la graisse — le moteur, en somme."""
        if self.masse_hors_graisse_kg is not None:
            return self.masse_hors_graisse_kg
        grasse = self.masse_grasse_kg
        return self.poids_kg - grasse if grasse is not None else None

    def to_row(self) -> dict[str, Any]:
        return {
            "jour": self.jour.isoformat(),
            "poids_kg": self.poids_kg,
            "imc": self.imc,
            "graisse_pct": self.graisse_pct,
            "muscle_squelettique_pct": self.muscle_squelettique_pct,
            "masse_hors_graisse_kg": self.masse_hors_graisse_kg,
            "gras_sous_cutane_pct": self.gras_sous_cutane_pct,
            "graisse_viscerale": self.graisse_viscerale,
            "eau_pct": self.eau_pct,
            "masse_musculaire_kg": self.masse_musculaire_kg,
            "masse_osseuse_kg": self.masse_osseuse_kg,
            "proteines_pct": self.proteines_pct,
            "metabolisme_base_kcal": self.metabolisme_base_kcal,
            "age_metabolique": self.age_metabolique,
            "source": self.source,
        }


def from_row(row: Any) -> Composition:
    """Reconstruit une pesée depuis une ligne de la base."""
    return Composition(
        jour=date.fromisoformat(row["jour"]),
        poids_kg=row["poids_kg"],
        imc=row["imc"],
        graisse_pct=row["graisse_pct"],
        muscle_squelettique_pct=row["muscle_squelettique_pct"],
        masse_hors_graisse_kg=row["masse_hors_graisse_kg"],
        gras_sous_cutane_pct=row["gras_sous_cutane_pct"],
        graisse_viscerale=row["graisse_viscerale"],
        eau_pct=row["eau_pct"],
        masse_musculaire_kg=row["masse_musculaire_kg"],
        masse_osseuse_kg=row["masse_osseuse_kg"],
        proteines_pct=row["proteines_pct"],
        metabolisme_base_kcal=row["metabolisme_base_kcal"],
        age_metabolique=row["age_metabolique"],
        source=row["source"] or "",
    )


# En deçà, l'écart entre deux moyennes tient au bruit de mesure plutôt qu'à une
# véritable évolution : hydratation, repas et heure de pesée suffisent à le
# produire.
SEUIL_BRUIT_KG = 0.3

# Repli sur un découpage en deux moitiés : il faut assez de pesées et assez de
# jours pour que chaque moitié ait un sens.
MIN_PESEES_REPLI = 4
MIN_JOURS_REPLI = 7


@dataclass
class Tendance:
    """Évolution entre deux moyennes, pour lisser le bruit d'une pesée isolée."""

    recent_kg: float
    precedent_kg: float | None
    jours_recents: int
    jours_precedents: int
    span_jours: int = 0  # étendue réellement couverte par la comparaison

    @property
    def delta_kg(self) -> float | None:
        if self.precedent_kg is None:
            return None
        return self.recent_kg - self.precedent_kg

    @property
    def lecture(self) -> str:
        delta = self.delta_kg
        if delta is None:
            return "pas encore assez de pesées pour dégager une tendance"
        if abs(delta) < SEUIL_BRUIT_KG:
            return "poids stable"
        sens = "en baisse" if delta < 0 else "en hausse"
        sur = f" sur {self.span_jours} jours" if self.span_jours else ""
        return f"{sens} de {abs(delta):.1f} kg{sur}"


def _moyenne(lot: list[Composition]) -> float:
    return sum(m.poids_kg for m in lot) / len(lot)


def tendance(mesures: list[Composition], fenetre_jours: int = 14) -> Tendance | None:
    """Dégage une direction du poids, en lissant le bruit d'une pesée isolée.

    Une pesée isolée ne dit presque rien : entre l'hydratation, le repas et
    l'heure, le chiffre bouge de plus d'un kilo d'un matin à l'autre. Deux
    moyennes, elles, montrent une direction.

    Deux découpages, dans cet ordre :

    1. la fenêtre des `fenetre_jours` derniers jours contre celle d'avant —
       c'est la lecture la plus stable dès qu'on a plusieurs semaines ;
    2. à défaut, quand toutes les pesées tiennent dans la fenêtre récente et
       qu'il n'y a donc rien derrière à quoi les comparer, la série est coupée
       en deux moitiés. Sans ce repli, quelqu'un qui se pèse assidûment pendant
       deux semaines n'obtient **aucune** tendance, alors que ses données en
       contiennent une — c'est précisément le cas au démarrage, quand la
       question intéresse le plus.
    """
    if not mesures:
        return None

    ordonnees = sorted(mesures, key=lambda m: m.jour)
    fin = ordonnees[-1].jour
    debut_recent = fin.toordinal() - fenetre_jours + 1
    debut_precedent = debut_recent - fenetre_jours

    recentes = [m for m in ordonnees if m.jour.toordinal() >= debut_recent]
    precedentes = [
        m for m in ordonnees if debut_precedent <= m.jour.toordinal() < debut_recent
    ]
    if recentes and precedentes:
        return Tendance(
            recent_kg=_moyenne(recentes),
            precedent_kg=_moyenne(precedentes),
            jours_recents=len(recentes),
            jours_precedents=len(precedentes),
            span_jours=fin.toordinal() - min(m.jour for m in precedentes).toordinal(),
        )

    etendue = fin.toordinal() - ordonnees[0].jour.toordinal()
    if len(ordonnees) >= MIN_PESEES_REPLI and etendue >= MIN_JOURS_REPLI:
        milieu = ordonnees[0].jour.toordinal() + etendue / 2
        premiere = [m for m in ordonnees if m.jour.toordinal() <= milieu]
        seconde = [m for m in ordonnees if m.jour.toordinal() > milieu]
        if premiere and seconde:
            return Tendance(
                recent_kg=_moyenne(seconde),
                precedent_kg=_moyenne(premiere),
                jours_recents=len(seconde),
                jours_precedents=len(premiere),
                span_jours=etendue,
            )

    return Tendance(
        recent_kg=_moyenne(recentes or ordonnees),
        precedent_kg=None,
        jours_recents=len(recentes or ordonnees),
        jours_precedents=0,
    )
