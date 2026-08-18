"""Génération des rapports Markdown et écriture sur disque."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

from .analysis import AnalyseSeance, Bilan, format_duration, format_pace
from .config import Config
from .plan import Adherence, Plan

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def majuscule(txt: str) -> str:
    """Met la première lettre en majuscule sans toucher au reste.

    `str.capitalize()` mettrait le reste en minuscules et transformerait
    « VFC sous la baseline » en « Vfc sous la baseline ».
    """
    return txt[:1].upper() + txt[1:] if txt else txt


def slug(txt: str) -> str:
    """Transforme un titre en nom de fichier sûr."""
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    txt = re.sub(r"[^\w\s-]", "", txt).strip().lower()
    return re.sub(r"[\s_-]+", "-", txt) or "rapport"


def ecrire(cfg: Config, nom: str, contenu: str) -> Path:
    """Écrit un rapport dans le dossier de rapports et renvoie son chemin."""
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    chemin = cfg.reports_dir / nom
    # Garde-fou : `nom` passe normalement par `nom_fichier`/`slug`, qui ne
    # laissent passer ni séparateur ni « .. » — mais un appelant futur pourrait
    # transmettre un nom non assaini. On refuse toute écriture hors du dossier.
    if chemin.resolve().parent != cfg.reports_dir.resolve():
        raise ValueError(f"nom de rapport invalide : {nom!r}")
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


# --------------------------------------------------------------------------
# Tableaux de bord
# --------------------------------------------------------------------------

def tableau_forme(bilan: Bilan) -> str:
    f = bilan.forme
    acwr = f"{f.acwr}" if f.acwr is not None else "—"
    lignes = [
        "| Indicateur | Valeur | Lecture |",
        "|---|---|---|",
        f"| Condition de fond (CTL) | {f.ctl} | charge chronique sur 42 jours |",
        f"| Fatigue récente (ATL) | {f.atl} | charge aiguë sur 7 jours |",
        f"| Fraîcheur (TSB) | {f.tsb:+} | {f.lecture_tsb} |",
        f"| Ratio de charge (ACWR) | {acwr} | {f.lecture_acwr} |",
    ]
    if bilan.historique_recent_court:
        lignes += [
            "",
            f"> ⚠️ Historique récent limité ({bilan.semaines_actives_recentes} semaine(s)"
            " active(s) sur les 4 dernières) : CTL et ACWR sont des ordres de grandeur,"
            " pas des mesures — l'ACWR sature mécaniquement quand toute la charge est"
            " récente.",
        ]
    return "\n".join(lignes)


def tableau_semaines(bilan: Bilan) -> str:
    lignes = [
        "| Semaine du | Séances | Distance | Temps | D+ | Charge |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for s in bilan.semaines:
        lignes.append(
            f"| {s.libelle} | {s.seances} | {s.distance_km:.1f} km |"
            f" {format_duration(s.duree_s)} | {s.denivele_m:.0f} m | {s.charge:.0f} |"
        )
    return "\n".join(lignes)


def barre(pourcentage: float, largeur: int = 20) -> str:
    """Petite barre de progression en caractères pleins."""
    remplis = int(round(pourcentage / 100 * largeur))
    return "█" * remplis + "░" * (largeur - remplis)


def bloc_intensite(bilan: Bilan) -> str:
    r = bilan.repartition
    if r.temps_total_s <= 0:
        return "_Pas de données de fréquence cardiaque exploitables sur la période._"
    return "\n".join(
        [
            f"- Facile (Z1-Z2) `{barre(r.facile_pct)}` **{r.facile_pct} %**",
            f"- Modéré (Z3)   `{barre(r.modere_pct)}` **{r.modere_pct} %**",
            f"- Dur (Z4-Z5)   `{barre(r.dur_pct)}` **{r.dur_pct} %**",
            "",
            f"_{majuscule(r.lecture)} (mesure basée sur les {r.base})._",
        ]
    )


def bloc_recuperation(bilan: Bilan) -> str:
    r = bilan.recuperation
    if not r.disponible:
        return (
            "_Aucune donnée de récupération synchronisée._\n\n"
            "Lance `coach sync` pour récupérer sommeil, VFC et readiness "
            "(activé par défaut ; `--sans-recup` pour l'ignorer)."
        )

    lignes = ["| Indicateur | Valeur |", "|---|---:|"]
    if r.sommeil_moyen_h is not None:
        detail = f"{r.sommeil_moyen_h} h"
        if r.dette_sommeil_h:
            detail += f" _(dette {r.dette_sommeil_h} h)_"
        lignes.append(f"| Sommeil moyen (7 j) | {detail} |")
    if r.vfc_moyenne_7j is not None:
        lignes.append(f"| VFC moyenne (7 j) | {r.vfc_moyenne_7j} ms |")
    if r.derive_fc_repos is not None:
        lignes.append(
            f"| FC de repos (7 j vs 28 j) | {r.fc_repos_7j} vs {r.fc_repos_28j} bpm"
            f" ({r.derive_fc_repos:+}) |"
        )
    dernier = r.dernier
    if dernier and dernier.readiness_score is not None:
        niveau = f" _{dernier.readiness_niveau}_" if dernier.readiness_niveau else ""
        lignes.append(f"| Readiness du jour | **{dernier.readiness_score}/100**{niveau} |")
    if dernier and dernier.body_battery_max is not None:
        lignes.append(
            f"| Body Battery | {dernier.body_battery_min}–{dernier.body_battery_max} |"
        )

    bloc = "\n".join(lignes)
    if r.alertes:
        bloc += "\n\n" + "\n".join(f"> ⚠️ {majuscule(a)}" for a in r.alertes)
    return bloc


def bloc_performances(bilan: Bilan) -> str:
    if not bilan.records and not bilan.projections:
        return "_Aucune sortie ne correspond encore à une distance de référence._"

    blocs = []
    if bilan.records:
        lignes = ["**Meilleures performances enregistrées**", "", "| Distance | Temps | Allure | Date |", "|---|---:|---:|---|"]
        for r in bilan.records:
            lignes.append(
                f"| {r.libelle} | {format_duration(r.temps_s)} |"
                f" {format_pace(r.allure_s_km)} | {r.quand:%d/%m/%Y} |"
            )
        blocs.append("\n".join(lignes))

    if bilan.projections:
        lignes = [
            f"**Projections** _(formule de Riegel, depuis {bilan.projections[0].source})_",
            "",
            "| Distance | Temps projeté | Allure |",
            "|---|---:|---:|",
        ]
        for p in bilan.projections:
            marque = " ⚠️" if p.extrapolation_lointaine else ""
            lignes.append(
                f"| {p.libelle}{marque} | {format_duration(p.temps_s)} | {format_pace(p.allure_s_km)} |"
            )
        if any(p.extrapolation_lointaine for p in bilan.projections):
            lignes.append("")
            lignes.append(
                "_⚠️ distance très éloignée de la référence : projection indicative,"
                " probablement optimiste._"
            )
        blocs.append("\n".join(lignes))

    return "\n\n".join(blocs)


def titre_vma(bilan: Bilan) -> str:
    """Complément du titre « Allures de référence » : la VMA et sa provenance.

    Une estimation ne doit jamais ressembler à une mesure : la fourchette, la
    source et la version du calcul l'accompagnent partout où elle s'affiche.
    """
    p = bilan.vma_provenance
    if p is None:
        return ""
    if p.mesuree:
        return f" — VMA {p.valeur} km/h ({p.source})"
    bas, haut = p.fourchette
    return (
        f" — VMA estimée entre {bas} et {haut} km/h "
        f"(source : {p.source} ; calcul {p.version})"
    )


def bloc_allures(bilan: Bilan) -> str:
    if not bilan.allures:
        return "_VMA inconnue : renseigne `vma_kmh` dans ta config, ou cours un 5 km chronométré._"
    lignes = ["| Type d'effort | Allure |", "|---|---:|"]
    lignes += [f"| {nom} | {valeur} |" for nom, valeur in bilan.allures.items()]
    if bilan.vma_provenance is not None and not bilan.vma_provenance.mesuree:
        lignes += [
            "",
            "> Estimation, pas une mesure : ces allures sont dérivées de la "
            "**borne basse** de la fourchette de VMA — en cas d'erreur, elles "
            "pèchent par douceur. Seul un test terrain ou labo fait foi ; "
            "renseigne `vma_kmh` dans ton profil dès que tu en as un.",
        ]
    if bilan.alerte_allures:
        lignes += ["", f"> ⚠️ {bilan.alerte_allures}"]
    return "\n".join(lignes)


def bloc_zones_fc(bilan: Bilan) -> str:
    """Les zones en battements par minute — la seule forme pilotable en courant."""
    if not bilan.zones_fc:
        return (
            "_FC max inconnue : renseigne `fc_max` dans ton profil pour obtenir "
            "tes zones. À défaut, la plus haute FC vue sur tes sorties en donne "
            "déjà une bonne approximation._"
        )
    lignes = ["| Zone | Fourchette | À quoi elle sert |", "|---|---:|---|"]
    usages = {
        "Z1": "récupération, échauffement, retour au calme",
        "Z2": "l'essentiel de ton volume — construit le moteur",
        "Z3": "tempo, allure soutenue mais tenable",
        "Z4": "seuil, ~allure 10 km, séances de qualité",
        "Z5": "VMA, fractions courtes et intenses",
    }
    for nom, (bas, haut) in bilan.zones_fc.items():
        code = nom.split(" ")[0]
        lignes.append(f"| {nom} | {bas}-{haut} bpm | {usages.get(code, '')} |")
    lignes += [
        "",
        "> Zones calculées depuis la FC max de ton profil. Si elle vient d'une "
        "observation en sortie et non d'un test maximal encadré, ta vraie FC max "
        "peut être un peu plus haute : les zones pécheraient alors par douceur — "
        "le bon côté de l'erreur.",
    ]
    return "\n".join(lignes)


def bloc_objectif(cfg: Config) -> str:
    objectif = cfg.athlete.prochain_objectif
    if not objectif:
        return "_Aucun objectif de course renseigné._"
    morceaux = [
        f"**{objectif.nom}** — {objectif.date:%d/%m/%Y} · {objectif.distance_km} km"
    ]
    if objectif.denivele_m:
        morceaux.append(f"D+ {objectif.denivele_m} m")
    if objectif.objectif_temps_s:
        allure = objectif.objectif_temps_s / objectif.distance_km
        morceaux.append(
            f"objectif {format_duration(objectif.objectif_temps_s)} ({format_pace(allure)})"
        )
    semaines = objectif.semaines_restantes
    morceaux.append(
        f"**dans {semaines:.1f} semaines**" if semaines >= 0 else "_déjà passée_"
    )
    return " · ".join(morceaux)


def bloc_composition(bilan: Bilan) -> str:
    """Poids et masse maigre, avec la tendance — seule vraiment lisible."""
    mesure = bilan.composition
    if mesure is None:
        return (
            "_Aucune pesée enregistrée._\n\n"
            "`coach poids --kg 72.4` en enregistre une (les mesures d'une balance "
            "à impédance s'ajoutent avec `--graisse`, `--muscle`, etc.)."
        )

    lignes = [
        "| Mesure | Valeur |",
        "|---|---:|",
        f"| Poids | **{mesure.poids_kg:.1f} kg** |",
    ]
    if mesure.imc is not None:
        lignes.append(f"| IMC | {mesure.imc:.1f} |")
    if mesure.graisse_pct is not None:
        masse = mesure.masse_grasse_kg
        detail = f"{mesure.graisse_pct:.1f} %"
        if masse is not None:
            detail += f" ({masse:.1f} kg)"
        lignes.append(f"| Masse grasse | {detail} |")
    maigre = mesure.masse_maigre_kg
    if maigre is not None:
        lignes.append(f"| Masse maigre | {maigre:.1f} kg |")
    if mesure.metabolisme_base_kcal is not None:
        lignes.append(f"| Métabolisme de base | {mesure.metabolisme_base_kcal} kcal/j |")

    parties = ["\n".join(lignes), ""]
    tendance = bilan.tendance_poids
    if tendance is not None:
        detail = f"{tendance.jours_recents + tendance.jours_precedents} pesées"
        if tendance.delta_kg is not None:
            detail += (
                f" · {tendance.precedent_kg:.1f} kg → {tendance.recent_kg:.1f} kg"
            )
        parties.append(f"_Tendance : **{tendance.lecture}** ({detail})._")
    parties.append(
        f"_Dernière pesée le {mesure.jour:%d/%m/%Y}"
        + (f" · {mesure.source}" if mesure.source else "")
        + "._"
    )
    if mesure.graisse_pct is not None:
        parties.append(
            "_Les pourcentages d'une balance à impédance varient avec "
            "l'hydratation et diffèrent d'un appareil à l'autre : c'est leur "
            "évolution à balance constante qui compte, pas leur valeur absolue._"
        )
    return "\n".join(parties)


# --------------------------------------------------------------------------
# Rapports complets
# --------------------------------------------------------------------------

def rapport_bilan(cfg: Config, bilan: Bilan, commentaire: str | None = None) -> str:
    """Le tableau de bord complet, avec l'analyse du coach si elle est fournie."""
    parties = [
        f"# Bilan d'entraînement — {cfg.athlete.prenom}",
        f"_Généré le {bilan.genere_le:%d/%m/%Y à %H:%M} · "
        f"{bilan.nb_activites} sorties de course enregistrées_",
        "",
        "## Objectif",
        bloc_objectif(cfg),
        "",
        "## État de forme",
        tableau_forme(bilan),
        "",
        "## Récupération",
        bloc_recuperation(bilan),
        "",
        "## Composition corporelle",
        bloc_composition(bilan),
        "",
        "## Volumes hebdomadaires",
        tableau_semaines(bilan),
        "",
        "## Répartition d'intensité (28 derniers jours)",
        bloc_intensite(bilan),
        "",
        "## Performances",
        bloc_performances(bilan),
        "",
        "## Zones de fréquence cardiaque",
        bloc_zones_fc(bilan),
        "",
        "## Allures de référence" + titre_vma(bilan),
        bloc_allures(bilan),
    ]

    if commentaire:
        parties += ["", "---", "", "## L'analyse du coach", "", commentaire]

    return "\n".join(parties) + "\n"


def rapport_debrief(
    cfg: Config, analyse: AnalyseSeance, bilan: Bilan, commentaire: str
) -> str:
    """Le débrief d'une séance."""
    a = analyse.activity
    entete = [
        f"# Débrief — {a.nom or 'Séance'} du {a.debut:%d/%m/%Y}",
        "",
        "| | |",
        "|---|---|",
        f"| Distance | **{a.distance_km:.2f} km** |",
        f"| Durée | **{format_duration(a.duree_effective_s)}** |",
        f"| Allure | **{format_pace(a.allure_s_km)}** |",
    ]
    if a.fc_moy:
        entete.append(f"| FC moyenne | {a.fc_moy} bpm"
                      + (f" (max {a.fc_max})" if a.fc_max else "") + " |")
    if analyse.zone_dominante:
        entete.append(f"| Zone dominante | {analyse.zone_dominante} |")
    if a.denivele_pos_m:
        entete.append(f"| Dénivelé positif | {a.denivele_pos_m:.0f} m |")
    if a.cadence_moy:
        entete.append(f"| Cadence | {a.cadence_moy} pas/min |")
    entete.append(f"| Charge | {analyse.charge} |")
    if analyse.decouplage_pct is not None:
        entete.append(f"| Dérive cardiaque | {analyse.decouplage_pct} % |")

    parties = entete + ["", "---", "", commentaire]

    if analyse.laps_resume:
        parties += ["", "---", "", "## Détail des tours", ""]
        parties += [f"- {t}" for t in analyse.laps_resume]

    parties += [
        "",
        "---",
        "",
        "## Contexte de charge",
        tableau_forme(bilan),
    ]
    return "\n".join(parties) + "\n"


ECART_VOLUME_TOLERE = 0.10  # au-delà, le plan est incohérent avec lui-même


def _ligne_volume(plan: Plan) -> str:
    """Volume de la semaine, calculé depuis les séances réellement planifiées.

    Le total annoncé par le coach n'est qu'un déclaratif : s'il s'écarte de la
    somme des séances, c'est le signe d'un plan incohérent et on le signale.
    """
    calcule = plan.volume_calcule_km
    nb = len([s for s in plan.seances if not s.est_repos])
    ligne = f"**Volume prévu :** {calcule:g} km · {nb} séances"

    declare = plan.volume_total_km
    if declare and calcule > 0 and abs(declare - calcule) / calcule > ECART_VOLUME_TOLERE:
        ligne += (
            f"\n\n> ⚠️ Le coach annonçait {declare:g} km pour la semaine, mais les"
            f" séances détaillées totalisent {calcule:g} km. Vérifie le plan avant"
            f" de le suivre."
        )
    return ligne


def rapport_plan(cfg: Config, plan: Plan, adherence: Adherence | None = None) -> str:
    """Le plan de la semaine, en Markdown lisible."""
    parties = [
        f"# {plan.titre}",
        f"_Semaine du {plan.debut:%d/%m/%Y} au {plan.fin:%d/%m/%Y} · "
        f"généré le {plan.genere_le:%d/%m/%Y à %H:%M}_",
        "",
        f"**Focus de la semaine :** {plan.focus}",
        "",
        plan.resume,
        "",
        _ligne_volume(plan),
        "",
    ]

    if plan.avertissements:
        parties += [
            "\n".join(f"> ⚠️ {majuscule(a)}." for a in plan.avertissements),
            "",
        ]

    parties += [
        "## Séances",
        "",
    ]

    for seance in plan.seances:
        jour = JOURS[seance.date.weekday()].capitalize()
        coche = "x" if seance.realisee else " "
        titre = f"### [{coche}] {jour} {seance.date:%d/%m} — {seance.titre}"
        parties.append(titre)

        meta = []
        if seance.distance_km:
            meta.append(f"{seance.distance_km:g} km")
        if seance.duree_min:
            meta.append(f"{seance.duree_min:g} min")
        meta.append(f"_{seance.type.replace('_', ' ')}_")
        parties.append(" · ".join(meta))

        if seance.objectif:
            parties.append(f"\n**Objectif :** {seance.objectif}")
        if seance.etapes:
            parties.append("")
            parties += [f"{i}. {e.resume()}" for i, e in enumerate(seance.etapes, 1)]
        if seance.consignes:
            parties.append(f"\n> {seance.consignes}")
        parties.append("")

    if plan.points_de_vigilance:
        parties += ["## Points de vigilance", ""]
        parties += [f"- {p}" for p in plan.points_de_vigilance]
        parties.append("")

    if adherence:
        parties += [
            "## Respect du plan précédent",
            "",
            f"- Séances réalisées : **{adherence.realisees}/{adherence.prevues}**"
            f" ({adherence.taux} %)",
            f"- Kilomètres : {adherence.km_realises} réalisés / {adherence.km_prevus} prévus"
            f" ({adherence.ecart_km:+} km)",
        ]
        if adherence.manquees:
            parties.append("- Séances manquées :")
            parties += [
                f"  - {s.date:%d/%m} — {s.titre}" for s in adherence.manquees
            ]
        parties.append("")

    return "\n".join(parties)


def bilan_vers_dict(cfg: Config, bilan: Bilan) -> dict:
    """Sérialise le bilan pour une consommation programmatique (JSON, MCP).

    C'est la même donnée que le rapport Markdown, sans la mise en forme : un
    agent ou un script la consomme sans avoir à analyser des tableaux.
    """
    f, r, rec = bilan.forme, bilan.repartition, bilan.recuperation
    objectif = cfg.athlete.prochain_objectif

    return {
        "genere_le": bilan.genere_le.isoformat(timespec="seconds"),
        "athlete": cfg.athlete.prenom,
        "nb_activites": bilan.nb_activites,
        "objectif": (
            {
                "nom": objectif.nom,
                "date": objectif.date.isoformat(),
                "distance_km": objectif.distance_km,
                "objectif_temps_s": objectif.objectif_temps_s,
                "semaines_restantes": round(objectif.semaines_restantes, 1),
            }
            if objectif
            else None
        ),
        "forme": {
            "ctl": f.ctl,
            "atl": f.atl,
            "tsb": f.tsb,
            "acwr": f.acwr,
            "lecture_tsb": f.lecture_tsb,
            "lecture_acwr": f.lecture_acwr,
        },
        "recuperation": {
            "disponible": rec.disponible,
            "jours_couverts": rec.jours_couverts,
            "sommeil_moyen_h": rec.sommeil_moyen_h,
            "dette_sommeil_h": rec.dette_sommeil_h,
            "vfc_moyenne_7j": rec.vfc_moyenne_7j,
            "vfc_jours_sous_baseline": rec.vfc_jours_sous_baseline,
            "fc_repos_7j": rec.fc_repos_7j,
            "fc_repos_28j": rec.fc_repos_28j,
            "derive_fc_repos": rec.derive_fc_repos,
            "readiness_score": rec.dernier.readiness_score if rec.dernier else None,
            "alertes": rec.alertes,
        },
        "semaines": [
            {
                "debut": s.debut.isoformat(),
                "seances": s.seances,
                "distance_km": s.distance_km,
                "duree_s": s.duree_s,
                "denivele_m": s.denivele_m,
                "charge": s.charge,
            }
            for s in bilan.semaines
        ],
        "repartition_intensite": {
            "facile_pct": r.facile_pct,
            "modere_pct": r.modere_pct,
            "dur_pct": r.dur_pct,
            "base": r.base,
            "lecture": r.lecture,
        },
        "vma_kmh": bilan.vma_kmh,
        "vma_provenance": (
            {
                "fourchette_kmh": list(bilan.vma_provenance.fourchette),
                "source": bilan.vma_provenance.source,
                "version_calculs": bilan.vma_provenance.version,
                "mesuree": bilan.vma_provenance.mesuree,
            }
            if bilan.vma_provenance
            else None
        ),
        "allures": bilan.allures,
        "records": [
            {
                "libelle": rec_.libelle,
                "temps_s": round(rec_.temps_s),
                "allure_s_km": round(rec_.allure_s_km),
                "date": rec_.quand.isoformat(),
            }
            for rec_ in bilan.records
        ],
        "projections": [
            {
                "libelle": p.libelle,
                "temps_s": round(p.temps_s),
                "allure_s_km": round(p.allure_s_km),
                "source": p.source,
            }
            for p in bilan.projections
        ],
        "derniere_seance": (
            {
                "activity_id": bilan.derniere_seance.activity.activity_id,
                "date": bilan.derniere_seance.activity.debut.isoformat(timespec="minutes"),
                "nom": bilan.derniere_seance.activity.nom,
                "distance_km": round(bilan.derniere_seance.activity.distance_km, 2),
                "duree_s": round(bilan.derniere_seance.activity.duree_effective_s),
                "allure_s_km": (
                    round(bilan.derniere_seance.activity.allure_s_km)
                    if bilan.derniere_seance.activity.allure_s_km
                    else None
                ),
                "fc_moy": bilan.derniere_seance.activity.fc_moy,
                "charge": bilan.derniere_seance.charge,
                "decouplage_pct": bilan.derniere_seance.decouplage_pct,
                "zone_dominante": bilan.derniere_seance.zone_dominante,
            }
            if bilan.derniere_seance
            else None
        ),
    }


def nom_fichier(kind: str, quand: date | datetime, suffixe: str = "") -> str:
    """Nom de fichier d'un rapport.

    Quand `quand` porte une heure (débrief d'une activité), elle entre dans le
    nom : deux activités homonymes le même jour — fréquent avec les noms Garmin
    par défaut comme « Course à pied » — ne doivent pas s'écraser en silence.
    Re-débriefer la même activité réutilise en revanche le même fichier.
    """
    horodatage = quand.strftime("%Y-%m-%d")
    if isinstance(quand, datetime):
        horodatage += quand.strftime("-%H%M")
    base = f"{horodatage}-{kind}"
    if suffixe:
        base += f"-{slug(suffixe)}"
    return f"{base}.md"
