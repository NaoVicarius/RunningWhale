"""Serveur MCP : expose le coach à Claude Desktop, claude.ai ou Claude Code.

Ce serveur ne fait **aucun** appel à un modèle : le client connecté est déjà le
modèle. Il expose des données et des calculs — état de forme, récupération,
détail des séances, allures, plan — pour que le modèle raisonne sur des chiffres
justes plutôt que d'estimer à vue de nez.

Lancement :  coach-mcp
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import analysis, plan as plan_mod, report
from .config import Config, ensure_dirs, load_config
from .db import Database
from .garmin import GarminError, connect, sync, sync_wellness

INSTRUCTIONS = """Ces outils donnent accès aux données d'entraînement en course à
pied de l'utilisateur, synchronisées depuis sa montre Garmin.

Toutes les métriques sont calculées localement à partir de ses activités réelles :
tu peux les citer telles quelles. Ne recalcule pas une charge, une moyenne ou une
projection toi-même — appelle l'outil correspondant, il est déjà juste.

Avant de te prononcer sur la fatigue ou sur l'opportunité d'une séance difficile,
regarde `etat_de_forme` (charge et fraîcheur) **et** `recuperation` (sommeil, VFC,
FC de repos) : les deux racontent des choses différentes et se contredisent parfois.

Tu es un coach, pas un médecin. Sur une douleur qui persiste ou un symptôme
inhabituel, oriente vers un professionnel de santé."""

serveur = MCPServer(
    name="runningwhale",
    title="RunningWhale — coach de running",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

_config: Config | None = None
_db: Database | None = None

# Bornes des paramètres exposés au modèle. Le client MCP est un modèle : il
# peut envoyer des valeurs absurdes (fenêtres de plusieurs siècles, plans de
# milliers de séances) qu'il faut refuser proprement plutôt que laisser
# déborder (OverflowError) ou remplir le disque.
MAX_JOURS = 3660  # dix ans : au-delà, la fenêtre n'a plus de sens
MAX_SEANCES_PLAN = 100  # plusieurs semaines de plan, largement
MAX_TAILLE_PLAN = 500_000  # octets de JSON : un plan réel pèse quelques Ko
FENETRE_PLAN_JOURS = 366  # une séance planifiée vit à moins d'un an d'aujourd'hui


def _borner_jours(jours: int, defaut: int) -> int:
    """Ramène une fenêtre en jours dans [1, MAX_JOURS]."""
    try:
        jours = int(jours)
    except (TypeError, ValueError):
        return defaut
    return max(1, min(jours, MAX_JOURS))


def _contexte() -> tuple[Config, Database]:
    """Configuration et base, chargées une seule fois."""
    global _config, _db
    if _config is None or _db is None:
        _config = load_config()
        ensure_dirs(_config)
        _db = Database(_config.db_path)
    return _config, _db


def _bilan(semaines: int = 8) -> tuple[Config, Database, analysis.Bilan]:
    cfg, db = _contexte()
    activites = db.activities()
    if not activites:
        raise ValueError(
            "Aucune activité en base. L'utilisateur doit lancer `coach sync`, "
            "ou tu peux appeler l'outil `synchroniser`."
        )
    recentes = [a for a in activites if a.debut.date() >= date.today() - timedelta(days=35)]
    detaillees = [db.get_activity(a.activity_id) or a for a in recentes]
    connus = {a.activity_id for a in detaillees}
    complet = detaillees + [a for a in activites if a.activity_id not in connus]
    complet.sort(key=lambda a: a.debut, reverse=True)

    bilan = analysis.bilan(
        complet,
        cfg.athlete,
        nb_semaines=semaines,
        wellness=db.wellness(depuis=date.today() - timedelta(days=35)),
    )
    return cfg, db, bilan


# --------------------------------------------------------------------------
# Outils de lecture
# --------------------------------------------------------------------------

@serveur.tool(
    description="État de forme actuel : charge chronique (CTL), fatigue récente (ATL), "
    "fraîcheur (TSB), ratio de charge (ACWR), plus les alertes de récupération. "
    "À appeler en premier pour situer l'athlète."
)
def etat_de_forme() -> dict[str, Any]:
    cfg, _, bilan = _bilan()
    f, rec = bilan.forme, bilan.recuperation
    return {
        "date": f.jour.isoformat(),
        "athlete": cfg.athlete.prenom,
        "ctl_condition_de_fond": f.ctl,
        "atl_fatigue_recente": f.atl,
        "tsb_fraicheur": f.tsb,
        "lecture_fraicheur": f.lecture_tsb,
        "acwr": f.acwr,
        "lecture_acwr": f.lecture_acwr,
        "alertes_recuperation": rec.alertes,
        "objectif": report.bilan_vers_dict(cfg, bilan)["objectif"],
    }


@serveur.tool(
    description="Bilan complet : forme, récupération, volumes hebdomadaires, "
    "répartition d'intensité, records, projections de temps et allures de référence."
)
def bilan_complet(semaines: int = 8) -> dict[str, Any]:
    cfg, _, bilan = _bilan(semaines=semaines)
    return report.bilan_vers_dict(cfg, bilan)


@serveur.tool(
    description="Signaux de récupération jour par jour : sommeil, VFC, FC de repos, "
    "Body Battery et readiness Garmin. Utile pour décider de forcer ou de lever le pied."
)
def recuperation(jours: int = 14) -> dict[str, Any]:
    _, db = _contexte()
    jours = _borner_jours(jours, defaut=14)
    donnees = db.wellness(depuis=date.today() - timedelta(days=jours))
    synthese = analysis.recuperation(donnees)
    return {
        "synthese": {
            "jours_couverts": synthese.jours_couverts,
            "sommeil_moyen_h": synthese.sommeil_moyen_h,
            "dette_sommeil_h": synthese.dette_sommeil_h,
            "vfc_moyenne_7j": synthese.vfc_moyenne_7j,
            "vfc_jours_sous_baseline": synthese.vfc_jours_sous_baseline,
            "fc_repos_7j": synthese.fc_repos_7j,
            "fc_repos_28j": synthese.fc_repos_28j,
            "derive_fc_repos": synthese.derive_fc_repos,
            "alertes": synthese.alertes,
            "lecture": synthese.lecture,
        },
        "jours": [
            {
                "date": w.jour.isoformat(),
                "sommeil_h": w.sommeil_h,
                "score_sommeil": w.score_sommeil,
                "vfc_ms": w.vfc_ms,
                "vfc_statut": w.vfc_statut,
                "vfc_sous_baseline": w.vfc_sous_baseline,
                "fc_repos": w.fc_repos,
                "readiness_score": w.readiness_score,
                "readiness_niveau": w.readiness_niveau,
                "body_battery_max": w.body_battery_max,
                "stress_moyen": w.stress_moyen,
            }
            for w in donnees
        ],
    }


@serveur.tool(
    description="Liste des sorties récentes avec distance, durée, allure, FC et charge. "
    "Pour le détail d'une séance précise (tours, dérive cardiaque), utiliser detail_seance."
)
def activites_recentes(jours: int = 21, limite: int = 30) -> list[dict[str, Any]]:
    cfg, db = _contexte()
    jours = _borner_jours(jours, defaut=21)
    limite = max(1, min(int(limite), 500))
    depuis = datetime.now() - timedelta(days=jours)
    activites = db.activities(depuis=depuis, seulement_course=True)[:limite]
    return [
        {
            "activity_id": a.activity_id,
            "date": a.debut.isoformat(timespec="minutes"),
            "nom": a.nom,
            "distance_km": round(a.distance_km, 2),
            "duree_s": round(a.duree_effective_s),
            "allure_s_km": round(a.allure_s_km) if a.allure_s_km else None,
            "allure": analysis.format_pace(a.allure_s_km),
            "fc_moy": a.fc_moy,
            "denivele_pos_m": a.denivele_pos_m,
            "charge": round(analysis.charge_seance(a, cfg.athlete), 1),
        }
        for a in activites
    ]


@serveur.tool(
    description="Détail d'une séance : tours, dérive cardiaque, gestion d'allure, "
    "zone d'intensité. Sans identifiant, renvoie la dernière course."
)
def detail_seance(activity_id: str | None = None) -> dict[str, Any]:
    cfg, db = _contexte()
    activite = db.get_activity(activity_id) if activity_id else db.last_activity()
    if activite is None:
        raise ValueError(
            f"Activité introuvable ({activity_id or 'aucune course en base'})."
        )

    a = analysis.analyse_seance(activite, cfg.athlete)
    return {
        "activity_id": activite.activity_id,
        "date": activite.debut.isoformat(timespec="minutes"),
        "nom": activite.nom,
        "type": activite.type,
        "distance_km": round(activite.distance_km, 2),
        "duree_s": round(activite.duree_effective_s),
        "allure": analysis.format_pace(activite.allure_s_km),
        "fc_moy": activite.fc_moy,
        "fc_max": activite.fc_max,
        "cadence_moy": activite.cadence_moy,
        "denivele_pos_m": activite.denivele_pos_m,
        "charge": a.charge,
        "zone_dominante": a.zone_dominante,
        "decouplage_pct": a.decouplage_pct,
        "lecture_decouplage": a.lecture_decouplage,
        "negative_split": a.negative_split,
        "ecart_allure_seuil": a.ecart_allure_seuil,
        "tours": [
            {
                "index": lap.index,
                "distance_m": lap.distance_m,
                "duree_s": lap.duree_s,
                "allure": analysis.format_pace(lap.allure_s_km),
                "fc_moy": lap.fc_moy,
            }
            for lap in activite.laps
        ],
    }


@serveur.tool(
    description="Allures d'entraînement de référence dérivées de la VMA, de "
    "l'endurance fondamentale à la VMA courte. À utiliser pour prescrire une séance."
)
def allures() -> dict[str, Any]:
    _, _, bilan = _bilan()
    return {
        "vma_kmh": bilan.vma_kmh,
        "allures": bilan.allures,
        "projections": [
            {
                "distance": p.libelle,
                "temps": analysis.format_duration(p.temps_s),
                "allure": analysis.format_pace(p.allure_s_km),
                "source": p.source,
                "extrapolation_lointaine": p.extrapolation_lointaine,
            }
            for p in bilan.projections
        ],
    }


@serveur.tool(
    description="Plan d'entraînement en cours et son taux de réalisation : "
    "ce qui était prévu face à ce qui a réellement été couru."
)
def plan_en_cours() -> dict[str, Any]:
    _, db = _contexte()
    brut = db.get_meta("plan_courant")
    if not brut:
        return {"plan": None, "message": "Aucun plan enregistré."}

    try:
        courant = plan_mod.Plan.from_json(brut)
    except (ValueError, KeyError, TypeError, AttributeError):
        # Entrée abîmée en base : un plan illisible ne doit pas casser l'outil.
        return {
            "plan": None,
            "message": "Le plan enregistré est illisible (données corrompues). "
            "Il faut en générer un nouveau.",
        }
    activites = db.activities(
        depuis=datetime.combine(courant.debut, datetime.min.time())
    )
    adherence = plan_mod.rapprocher(courant, activites)

    return {
        "titre": courant.titre,
        "focus": courant.focus,
        "debut": courant.debut.isoformat(),
        "fin": courant.fin.isoformat(),
        "volume_prevu_km": courant.volume_calcule_km,
        "seances": [
            {
                "date": s.date.isoformat(),
                "titre": s.titre,
                "type": s.type,
                "distance_km": s.distance_km,
                "duree_min": s.duree_min,
                "objectif": s.objectif,
                "consignes": s.consignes,
                "etapes": [e.resume() for e in s.etapes],
                "realisee": s.realisee,
            }
            for s in courant.seances
        ],
        "points_de_vigilance": courant.points_de_vigilance,
        "adherence": {
            "prevues": adherence.prevues,
            "realisees": adherence.realisees,
            "taux": adherence.taux,
            "km_prevus": adherence.km_prevus,
            "km_realises": adherence.km_realises,
            "manquees": [
                {"date": s.date.isoformat(), "titre": s.titre} for s in adherence.manquees
            ],
        },
    }


# --------------------------------------------------------------------------
# Outils d'écriture
# --------------------------------------------------------------------------

@serveur.tool(
    description="Synchronise les dernières activités et données de récupération "
    "depuis Garmin Connect. À appeler si les données semblent dater."
)
def synchroniser(jours: int = 30, avec_recuperation: bool = True) -> dict[str, Any]:
    cfg, db = _contexte()
    jours = _borner_jours(jours, defaut=30)
    try:
        api = connect(cfg.token_dir)
        resultat = sync(api, db, depuis=date.today() - timedelta(days=jours), verbose=False)
    except GarminError as exc:
        raise ValueError(
            f"Synchronisation impossible : {exc}. L'utilisateur doit peut-être "
            "relancer `coach login` dans son terminal."
        ) from exc

    sortie = {
        "nouvelles_activites": [
            {
                "activity_id": a.activity_id,
                "date": a.debut.isoformat(timespec="minutes"),
                "nom": a.nom,
                "distance_km": round(a.distance_km, 2),
            }
            for a in resultat.nouvelles
        ],
        "total_en_base": db.count_activities(),
    }

    if avec_recuperation:
        try:
            sortie["jours_recuperation"] = sync_wellness(api, db, jours=min(jours, 30))
        except Exception as exc:  # noqa: BLE001 — ne doit pas invalider la synchro
            sortie["recuperation_erreur"] = str(exc)

    return sortie


@serveur.tool(
    description="Enregistre un plan d'entraînement que tu viens de concevoir. "
    "Il devient le plan courant, exportable en calendrier ou vers la montre "
    "avec `coach plan --montrer`, `--ics` et `--push`. Le format attendu est celui "
    "renvoyé par plan_en_cours : un objet avec titre, focus, resume, seances "
    "(date AAAA-MM-JJ, titre, type, distance_km, duree_min, objectif, consignes, "
    "etapes) et points_de_vigilance."
)
def enregistrer_plan(plan: dict[str, Any]) -> dict[str, Any]:
    cfg, db = _contexte()

    seances_brutes = plan.get("seances") if isinstance(plan, dict) else None
    if not isinstance(seances_brutes, list) or not all(
        isinstance(s, dict) for s in seances_brutes
    ):
        raise ValueError("Plan mal formé : `seances` doit être une liste d'objets.")
    if len(seances_brutes) > MAX_SEANCES_PLAN:
        raise ValueError(
            f"Trop de séances ({len(seances_brutes)}) : {MAX_SEANCES_PLAN} au maximum."
        )
    if len(json.dumps(plan, ensure_ascii=False, default=str)) > MAX_TAILLE_PLAN:
        raise ValueError(
            "Plan démesuré : un plan hebdomadaire réel pèse quelques kilo-octets."
        )
    # `Plan.from_payload` remplace une date illisible par aujourd'hui : ici la
    # donnée vient d'un client externe, mieux vaut refuser que corriger en silence.
    for s in seances_brutes:
        brut = s.get("date")
        try:
            quand = datetime.strptime(str(brut)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            raise ValueError(f"Date de séance illisible : {brut!r} (attendu AAAA-MM-JJ).")
        if abs((quand - date.today()).days) > FENETRE_PLAN_JOURS:
            raise ValueError(
                f"Date de séance invraisemblable : {quand.isoformat()}"
                " (à plus d'un an d'aujourd'hui)."
            )

    try:
        nouveau = plan_mod.Plan.from_payload(plan)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"Plan mal formé : {exc}") from exc

    if not nouveau.seances:
        raise ValueError("Le plan ne contient aucune séance.")

    # Mêmes garde-fous sémantiques que `coach plan` : le schéma contraint la
    # forme, pas le sens, et un plan conçu dans la conversation ne doit pas
    # contourner les contrôles appliqués aux plans générés par le CLI.
    vma = volume_recent = None
    try:
        _, _, bilan = _bilan()
        vma = bilan.vma_kmh
        volume_recent = max((s.distance_km for s in bilan.semaines[-4:]), default=0.0)
    except ValueError:
        pass  # base vide : pas de référence pour les contrôles d'allure et de volume
    nouveau.avertissements = plan_mod.controler_plan(
        nouveau, debut=nouveau.debut, vma_kmh=vma, volume_recent_km=volume_recent
    )

    contenu = report.rapport_plan(cfg, nouveau)
    chemin = report.ecrire(cfg, report.nom_fichier("plan", nouveau.debut), contenu)
    db.save_report("plan", contenu, chemin=str(chemin))
    db.set_meta("plan_courant", nouveau.to_json())

    return {
        "enregistre": True,
        "avertissements": nouveau.avertissements,
        "titre": nouveau.titre,
        "debut": nouveau.debut.isoformat(),
        "fin": nouveau.fin.isoformat(),
        "seances": len(nouveau.seances),
        "volume_km": nouveau.volume_calcule_km,
        "rapport": str(chemin),
    }


def main() -> int:
    """Point d'entrée du serveur, en transport stdio."""
    try:
        serveur.run(transport="stdio")
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
