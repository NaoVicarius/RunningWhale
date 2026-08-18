"""Le coach : construction du contexte et appels à l'API Claude.

Le contexte envoyé au modèle est toujours factuel — il est produit par
`analysis.py` à partir des données Garmin réelles. Le modèle interprète,
il n'invente pas les chiffres.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import anthropic

from .analysis import (
    AnalyseSeance,
    Bilan,
    format_duration,
    format_pace,
)
from .config import Config
from .plan import SCHEMA_PLAN, Adherence, Plan, controler_plan

MAX_TOKENS_TEXTE = 32_000
MAX_TOKENS_PLAN = 16_000
BETA_FALLBACK = "server-side-fallback-2026-07-01"


class CoachError(RuntimeError):
    """Erreur d'appel au modèle."""


SYSTEM_COACH = """Tu es le coach de course à pied personnel de {prenom}.

Tu t'appuies exclusivement sur les données de sa montre Garmin, qui te sont
fournies sous forme de métriques calculées. Ces chiffres viennent de mesures
réelles : cite-les et raisonne dessus, en tenant compte des réserves de
fiabilité que le contexte signale (historique trop court, données manquantes).
En revanche, ne fabrique jamais une donnée absente — si une
information manque pour trancher, dis-le en une phrase et propose ce qu'il
faudrait mesurer.

Ton rôle :
- lire la charge, la fraîcheur et la répartition d'intensité pour dire où en est l'athlète ;
- relier chaque conseil à l'objectif de course visé et au temps restant ;
- prévenir la blessure autant que chercher la performance — une montée de charge
  trop rapide, une dérive cardiaque anormale ou un manque d'endurance fondamentale
  méritent d'être signalés tôt.

Ta manière de t'exprimer :
- Commence par la conclusion : la première phrase dit où en est l'athlète ou ce
  qu'il faut faire. Le détail et le raisonnement viennent ensuite.
- Écris en {langue}, en phrases complètes, en t'adressant directement à l'athlète.
  Tutoie-le.
- Sois précis et concis. Ne remplis pas de sections vides, ne répète pas les
  chiffres déjà affichés dans le rapport sans les interpréter, n'ajoute pas de
  mise en garde générique sur la consultation d'un médecin à chaque réponse.
- Une allure se donne toujours au format m:ss/km, une fréquence cardiaque en bpm.
- Tu es un coach, pas un médecin : sur une douleur qui persiste ou un symptôme
  inhabituel, oriente vers un professionnel de santé, une fois, sans insister.
"""


def _client(cfg: Config) -> anthropic.Anthropic:
    if not cfg.anthropic_api_key:
        raise CoachError(
            "ANTHROPIC_API_KEY n'est pas définie. Exporte ta clé API Anthropic "
            "avant d'utiliser les commandes de coaching."
        )
    return anthropic.Anthropic(api_key=cfg.anthropic_api_key)


def _systeme(cfg: Config) -> list[dict[str, Any]]:
    """Prompt système, marqué pour la mise en cache (il est stable d'un appel à l'autre).

    Note d'échelle : sur claude-opus-5 le préfixe minimal cachable est de
    512 tokens ; ce prompt (~500 tokens) passe silencieusement sous le seuil et
    n'est donc pas caché aujourd'hui. Le marqueur est inoffensif et prendra
    effet si le prompt grossit — le contexte factuel, lui, change à chaque
    appel et ne serait de toute façon jamais réutilisé.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_COACH.format(
                prenom=cfg.athlete.prenom, langue=cfg.langue
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _appeler(
    cfg: Config,
    messages: list[dict[str, Any]],
    max_tokens: int,
    output_format: dict[str, Any] | None = None,
) -> str:
    """Appelle le modèle en streaming et renvoie le texte de la réponse."""
    client = _client(cfg)

    output_config: dict[str, Any] = {"effort": cfg.effort}
    if output_format is not None:
        output_config["format"] = output_format

    kwargs: dict[str, Any] = {
        "model": cfg.modele,
        "max_tokens": max_tokens,
        "system": _systeme(cfg),
        "messages": messages,
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
    }

    message = _stream_avec_repli(client, kwargs)

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        motif = getattr(details, "explanation", None) or "aucun motif fourni"
        raise CoachError(f"Le modèle a décliné la demande ({motif}).")

    texte = "".join(
        bloc.text for bloc in message.content if getattr(bloc, "type", "") == "text"
    )
    if not texte.strip():
        raise CoachError("Le modèle n'a renvoyé aucun texte exploitable.")
    return texte


def _stream_avec_repli(client: anthropic.Anthropic, kwargs: dict[str, Any]) -> Any:
    """Tente l'appel avec repli serveur activé, sinon retombe sur l'appel standard.

    Le repli serveur (`fallbacks`) fait rejouer la requête sur un autre modèle si
    les classificateurs de sécurité la déclinent. Le paramètre est récent : si le
    SDK ou l'API installés ne le connaissent pas, on refait l'appel sans lui.
    """
    try:
        with client.beta.messages.stream(
            **kwargs, betas=[BETA_FALLBACK], fallbacks="default"
        ) as flux:
            return flux.get_final_message()
    except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
        pass
    except anthropic.APIStatusError as exc:
        if exc.status_code not in (400, 404):
            raise CoachError(f"Appel au modèle impossible : {exc}") from exc

    try:
        with client.messages.stream(**kwargs) as flux:
            return flux.get_final_message()
    except anthropic.APIError as exc:
        raise CoachError(f"Appel au modèle impossible : {exc}") from exc


# --------------------------------------------------------------------------
# Construction du contexte factuel
# --------------------------------------------------------------------------

def contexte_athlete(cfg: Config) -> str:
    a = cfg.athlete
    lignes = [f"## Profil — {a.prenom}"]
    champs = [
        ("Âge", a.age),
        ("Sexe", a.sexe),
        ("Poids", f"{a.poids_kg} kg" if a.poids_kg else None),
        ("FC max", f"{a.fc_max} bpm" if a.fc_max else None),
        ("FC repos", f"{a.fc_repos} bpm" if a.fc_repos else None),
        ("FC seuil", f"{a.fc_seuil} bpm" if a.fc_seuil else None),
        ("Allure seuil", format_pace(a.allure_seuil_s_km) if a.allure_seuil_s_km else None),
        ("Séances/semaine visées", a.seances_par_semaine),
        ("Volume hebdo cible", f"{a.volume_hebdo_cible_km} km" if a.volume_hebdo_cible_km else None),
    ]
    lignes += [f"- {nom} : {valeur}" for nom, valeur in champs if valeur]
    if a.historique:
        lignes.append(f"- Antécédents / blessures : {a.historique}")
    if a.contraintes:
        lignes.append(f"- Contraintes : {a.contraintes}")

    if a.objectifs:
        lignes.append("\n## Objectifs")
        for r in a.objectifs:
            morceaux = [f"{r.nom} — {r.date:%d/%m/%Y} — {r.distance_km} km"]
            if r.objectif_temps_s:
                morceaux.append(f"objectif {format_duration(r.objectif_temps_s)}")
            if r.denivele_m:
                morceaux.append(f"D+ {r.denivele_m} m")
            semaines = r.semaines_restantes
            if semaines >= 0:
                morceaux.append(f"dans {semaines:.1f} semaines")
            else:
                morceaux.append("déjà passée")
            lignes.append(f"- [{r.priorite}] " + " · ".join(morceaux))
    else:
        lignes.append("\n## Objectifs\n- Aucun objectif de course renseigné.")

    return "\n".join(lignes)


def contexte_bilan(bilan: Bilan) -> str:
    """Rend le bilan chiffré sous forme de texte compact pour le modèle."""
    f = bilan.forme
    lignes = [
        "## État de forme",
        f"- Charge chronique (CTL, condition de fond) : {f.ctl}",
        f"- Charge aiguë (ATL, fatigue récente) : {f.atl}",
        f"- Fraîcheur (TSB = CTL − ATL) : {f.tsb} → {f.lecture_tsb}",
        f"- Ratio charge aiguë/chronique (ACWR) : {f.acwr if f.acwr is not None else 'non calculable'}"
        f" → {f.lecture_acwr}",
        f"- Activités de course enregistrées : {bilan.nb_activites}",
    ]
    if bilan.historique_recent_court:
        lignes.append(
            f"- ⚠ Historique récent limité : {bilan.semaines_actives_recentes} semaine(s)"
            " avec des sorties sur les 4 dernières. CTL, ACWR et projections sont"
            " mécaniquement peu fiables sur une fenêtre aussi creuse : présente-les"
            " comme indicatifs et ne fonde pas d'alerte de surcharge sur le seul ACWR."
        )

    lignes.append("\n## Volumes hebdomadaires")
    lignes.append("| Semaine du | Séances | Distance | Temps | D+ | Charge |")
    lignes.append("|---|---|---|---|---|---|")
    for s in bilan.semaines:
        lignes.append(
            f"| {s.libelle} | {s.seances} | {s.distance_km} km |"
            f" {format_duration(s.duree_s)} | {s.denivele_m:.0f} m | {s.charge} |"
        )

    rec = bilan.recuperation
    if rec.disponible:
        lignes.append(f"\n## Récupération ({rec.jours_couverts} jours de données)")
        if rec.sommeil_moyen_h is not None:
            lignes.append(f"- Sommeil moyen sur 7 jours : {rec.sommeil_moyen_h} h")
        if rec.dette_sommeil_h:
            lignes.append(f"- Dette de sommeil cumulée sur 7 jours : {rec.dette_sommeil_h} h")
        if rec.vfc_moyenne_7j is not None:
            lignes.append(f"- VFC moyenne sur 7 jours : {rec.vfc_moyenne_7j} ms")
        if rec.vfc_jours_sous_baseline:
            lignes.append(
                f"- VFC sous la fourchette habituelle depuis {rec.vfc_jours_sous_baseline} jours"
            )
        if rec.derive_fc_repos is not None:
            lignes.append(
                f"- FC de repos : {rec.fc_repos_7j} bpm sur 7 jours contre"
                f" {rec.fc_repos_28j} bpm sur 28 jours ({rec.derive_fc_repos:+} bpm)"
            )
        dernier = rec.dernier
        if dernier and dernier.readiness_score is not None:
            lignes.append(
                f"- Readiness Garmin du {dernier.jour:%d/%m} : {dernier.readiness_score}/100"
                + (f" ({dernier.readiness_niveau})" if dernier.readiness_niveau else "")
            )
        if rec.alertes:
            lignes.append("- Alertes de récupération :")
            lignes += [f"  - {a}" for a in rec.alertes]
        else:
            lignes.append("- Aucune alerte de récupération.")
    else:
        lignes.append(
            "\n## Récupération\n- Aucune donnée synchronisée (sommeil, VFC, readiness)."
            " Ne te prononce pas sur la récupération autrement qu'à partir de la charge."
        )

    r = bilan.repartition
    if r.temps_total_s > 0:
        lignes += [
            "\n## Répartition d'intensité (28 derniers jours)",
            f"- Facile (Z1-Z2) : {r.facile_pct} %",
            f"- Modéré (Z3) : {r.modere_pct} %",
            f"- Dur (Z4-Z5) : {r.dur_pct} %",
            f"- Mesure basée sur les {r.base} ; lecture : {r.lecture}",
        ]
    else:
        lignes += [
            "\n## Répartition d'intensité (28 derniers jours)",
            "- Aucune donnée de fréquence cardiaque sur la période : la répartition"
            " d'intensité n'est pas mesurable. Ne commente ni les zones ni la"
            " polarisation ; raisonne sur les allures et les volumes.",
        ]

    if bilan.vma_provenance:
        p = bilan.vma_provenance
        if p.mesuree:
            lignes.append(f"\n## VMA : {p.valeur} km/h ({p.source})")
        else:
            bas, haut = p.fourchette
            lignes += [
                f"\n## VMA estimée : entre {bas} et {haut} km/h",
                f"- Source : {p.source} (calcul {p.version}).",
                "- C'est une estimation, pas une mesure : ne la présente jamais "
                "comme un acquis, et ne fonde aucune prescription sur sa borne "
                "haute. Les allures ci-dessous sont déjà dérivées de la borne "
                "basse. Si l'athlète évoque un test terrain ou labo, "
                "recommande-lui d'en reporter le résultat dans son profil.",
            ]
    if bilan.zones_fc:
        lignes.append("\n## Zones de fréquence cardiaque (en bpm)")
        lignes += [
            f"- {nom} : {bas}-{haut} bpm" for nom, (bas, haut) in bilan.zones_fc.items()
        ]
        lignes.append(
            "- Zones dérivées de la FC max du profil, pas d'un test en labo : "
            "traite-les comme des repères, à recaler si les sensations de "
            "l'athlète les contredisent."
        )

    if bilan.allures:
        lignes.append("\n## Allures d'entraînement de référence")
        lignes += [f"- {nom} : {valeur}" for nom, valeur in bilan.allures.items()]
        if bilan.alerte_allures:
            lignes.append(
                f"- ⚠️ ATTENTION : {bilan.alerte_allures} Ne prescris aucune "
                "allure sous cette vitesse : elle serait marchée, pas courue."
            )

    if bilan.records:
        lignes.append("\n## Meilleures performances enregistrées")
        for rec in bilan.records:
            lignes.append(
                f"- {rec.libelle} : {format_duration(rec.temps_s)}"
                f" ({format_pace(rec.allure_s_km)}) le {rec.quand:%d/%m/%Y}"
            )

    if bilan.projections:
        base = bilan.projections[0].source
        lignes.append(f"\n## Projections de temps (Riegel, depuis {base})")
        for p in bilan.projections:
            marque = (
                " — extrapolation lointaine, probablement optimiste"
                if p.extrapolation_lointaine
                else ""
            )
            lignes.append(
                f"- {p.libelle} : {format_duration(p.temps_s)} ({format_pace(p.allure_s_km)}){marque}"
            )

    return "\n".join(lignes)


def contexte_seance(analyse: AnalyseSeance) -> str:
    a = analyse.activity
    lignes = [
        f"## Séance du {a.debut:%A %d/%m/%Y à %H:%M}",
        f"- Nom : {a.nom or '(sans titre)'} · Type : {a.type}",
        f"- Distance : {a.distance_km:.2f} km · Durée : {format_duration(a.duree_effective_s)}"
        f" · Allure : {format_pace(a.allure_s_km)}",
    ]
    if a.denivele_pos_m:
        lignes.append(f"- Dénivelé positif : {a.denivele_pos_m:.0f} m")
    if a.fc_moy:
        lignes.append(
            f"- FC moyenne : {a.fc_moy} bpm"
            + (f" · FC max : {a.fc_max} bpm" if a.fc_max else "")
            + (f" · Zone dominante : {analyse.zone_dominante}" if analyse.zone_dominante else "")
        )
    if a.cadence_moy:
        lignes.append(f"- Cadence moyenne : {a.cadence_moy} pas/min")
    if a.puissance_moy:
        lignes.append(f"- Puissance moyenne : {a.puissance_moy} W")
    if a.temperature_c is not None:
        lignes.append(f"- Température : {a.temperature_c:.0f} °C")
    if a.training_effect_aerobie:
        lignes.append(
            f"- Effet d'entraînement Garmin : aérobie {a.training_effect_aerobie}"
            + (f" · anaérobie {a.training_effect_anaerobie}" if a.training_effect_anaerobie else "")
        )

    lignes.append(f"- Charge calculée : {analyse.charge}")
    lignes.append(f"- Dérive cardiaque : {analyse.lecture_decouplage}")
    if analyse.decouplage_pct is not None:
        lignes.append(f"  (valeur mesurée : {analyse.decouplage_pct} %)")
    if analyse.negative_split is not None:
        lignes.append(
            "- Gestion d'allure : "
            + ("seconde moitié plus rapide (negative split)" if analyse.negative_split
               else "seconde moitié plus lente que la première")
        )
    if analyse.ecart_allure_seuil:
        lignes.append(f"- Par rapport au seuil : {analyse.ecart_allure_seuil}")

    if analyse.laps_resume:
        lignes.append("\n### Détail des tours")
        lignes += [f"- {t}" for t in analyse.laps_resume]

    return "\n".join(lignes)


def contexte_adherence(adherence: Adherence | None) -> str:
    if adherence is None:
        return ""
    lignes = [
        "\n## Respect du plan précédent",
        f"- Séances réalisées : {adherence.realisees}/{adherence.prevues} ({adherence.taux} %)",
        f"- Kilomètres : {adherence.km_realises} réalisés pour {adherence.km_prevus} prévus"
        f" (écart {adherence.ecart_km:+} km)",
    ]
    if adherence.manquees:
        lignes.append("- Séances manquées :")
        lignes += [f"  - {s.date:%d/%m} — {s.titre}" for s in adherence.manquees]
    if adherence.hors_plan:
        lignes.append(f"- Sorties hors plan : {len(adherence.hors_plan)}")
    return "\n".join(lignes)


# --------------------------------------------------------------------------
# Points d'entrée du coach
# --------------------------------------------------------------------------

def debrief_seance(cfg: Config, bilan: Bilan, analyse: AnalyseSeance) -> str:
    """Analyse d'une séance, replacée dans le contexte de la charge et de l'objectif."""
    prompt = f"""Voici les données de la dernière séance de {cfg.athlete.prenom},
et son contexte d'entraînement.

{contexte_athlete(cfg)}

{contexte_seance(analyse)}

{contexte_bilan(bilan)}

Rédige le débrief de cette séance en Markdown, sans titre de niveau 1, avec :
1. **Ce qu'il faut retenir** — deux ou trois phrases : la séance était-elle réussie
   au regard de son intention apparente, et qu'est-ce qu'elle dit de la forme actuelle.
2. **Ce que disent les chiffres** — l'interprétation des métriques marquantes de
   cette séance (allure, dérive cardiaque, gestion d'effort, zone d'intensité).
   Ne recopie pas la totalité des données : commente celles qui portent un signal.
3. **Pour la suite** — ce que cette séance implique concrètement pour les prochains
   jours : récupération nécessaire, ajustement d'allure ou de volume.

Si un signal mérite une vigilance particulière (charge, dérive, écart d'allure
inhabituel), dis-le explicitement plutôt que de le noyer dans le texte."""

    return _appeler(cfg, [{"role": "user", "content": prompt}], MAX_TOKENS_TEXTE)


def bilan_periodique(cfg: Config, bilan: Bilan) -> str:
    """Point d'étape sur plusieurs semaines."""
    prompt = f"""Fais le point sur l'entraînement de {cfg.athlete.prenom}.

{contexte_athlete(cfg)}

{contexte_bilan(bilan)}

Rédige un bilan en Markdown, sans titre de niveau 1, avec :
1. **Où tu en es** — l'état de forme actuel et la tendance des dernières semaines.
2. **Ce qui fonctionne** — les points solides de la préparation, chiffres à l'appui.
3. **Ce qui coince** — les déséquilibres réels (répartition d'intensité, progression
   de charge, régularité), classés par importance. S'il n'y en a pas, dis-le.
4. **Cap pour les prochaines semaines** — l'orientation à prendre au regard de
   l'objectif de course visé et du temps restant."""

    return _appeler(cfg, [{"role": "user", "content": prompt}], MAX_TOKENS_TEXTE)


def prompt_plan_semaine(
    cfg: Config,
    bilan: Bilan,
    debut: date,
    adherence: Adherence | None = None,
    consignes: str = "",
) -> str:
    """Assemble la consigne complète de construction du plan.

    Séparée de l'appel réseau, pour que la consigne puisse être lue et traitée
    ailleurs — par un agent qui parle déjà à l'athlète, par exemple, et qui n'a
    aucune raison de repasser par l'API pour produire ce même JSON. Le plan
    obtenu rentre par `plan_depuis_payload` et subit exactement les mêmes
    contrôles.
    """
    a = cfg.athlete
    objectif = a.prochain_objectif
    semaine_de_course = (
        objectif is not None and debut <= objectif.date <= debut + timedelta(days=6)
    )
    cadrage = []
    if objectif:
        cadrage.append(
            f"L'objectif est {objectif.nom} ({objectif.distance_km} km) le "
            f"{objectif.date:%d/%m/%Y}, soit dans {objectif.semaines_restantes:.1f} semaines."
        )
        if semaine_de_course:
            cadrage.append(
                "La course tombe pendant la semaine planifiée : c'est une semaine"
                " d'affûtage, pas d'entraînement. Planifie la course elle-même à sa"
                " date (type `course`), réduis nettement le volume des jours qui la"
                " précèdent, ne programme aucune séance dure dans les trois jours"
                " avant la course, et mets du repos ou de la récupération juste après."
                " Les cibles habituelles de volume et de nombre de séances ne"
                " s'appliquent pas cette semaine."
            )
    # En semaine de course, les cibles hebdomadaires habituelles contrediraient
    # l'affûtage : on ne les envoie pas.
    if a.seances_par_semaine and not semaine_de_course:
        cadrage.append(f"Vise {a.seances_par_semaine} séances sur la semaine.")
    if a.volume_hebdo_cible_km and not semaine_de_course:
        cadrage.append(f"Volume hebdomadaire cible : {a.volume_hebdo_cible_km} km.")
    if consignes:
        cadrage.append(f"Consigne particulière de l'athlète : {consignes}")

    prompt = f"""Construis le plan d'entraînement de la semaine du {debut:%d/%m/%Y}
pour {cfg.athlete.prenom}.

{contexte_athlete(cfg)}

{contexte_bilan(bilan)}
{contexte_adherence(adherence)}

{" ".join(cadrage)}

Règles de construction :
- La progression de charge par rapport aux semaines précédentes doit rester
  maîtrisée : vise un ACWR entre 0,8 et 1,3. Si la fraîcheur actuelle est basse
  ou l'ACWR déjà élevé, planifie une semaine d'allègement et dis-le dans le résumé.
- Donne une date à chaque séance, y compris les jours de repos.
- Chaque séance de qualité doit avoir un échauffement et un retour au calme
  explicites dans ses étapes.
- Les allures sont exprimées au format m:ss/km et doivent être cohérentes avec
  les allures de référence calculées plus haut.
- Le champ `consignes` s'adresse directement à l'athlète : dis-lui comment
  exécuter la séance et à quoi faire attention.
- OBLIGATOIRE : chaque séance courue doit indiquer, dans `consignes`, la zone de
  fréquence cardiaque visée ET sa fourchette en bpm, prise dans le tableau des
  zones ci-dessus (par exemple « Z2 endurance fondamentale, 141-153 bpm »). Une
  consigne d'allure seule n'est pas exécutable : la chaleur, le vent et la
  fatigue déplacent l'allure à effort égal, pas la fréquence cardiaque.
- Le champ `resume` justifie le plan : pourquoi cette charge, pourquoi ces séances,
  compte tenu de son état de forme et de l'échéance."""
    return prompt


def plan_depuis_payload(payload: dict[str, Any], debut: date, bilan: Bilan) -> Plan:
    """Construit un `Plan` depuis un JSON déjà produit, et le contrôle.

    Même sortie que `plan_semaine`, sans l'appel réseau : `from_payload` valide
    la forme, `controler_plan` juge le sens — progression de charge, cohérence
    des allures, séances dures trop rapprochées. Un plan écrit à la main ou par
    un agent passe donc les mêmes garde-fous que celui qu'aurait rendu l'API.
    """
    plan = Plan.from_payload(payload)
    plan.avertissements = controler_plan(
        plan,
        debut=debut,
        vma_kmh=bilan.vma_kmh,
        volume_recent_km=max((s.distance_km for s in bilan.semaines[-4:]), default=0.0),
    )
    return plan


def plan_semaine(
    cfg: Config,
    bilan: Bilan,
    debut: date,
    adherence: Adherence | None = None,
    consignes: str = "",
) -> Plan:
    """Produit le plan de la semaine sous forme structurée (JSON contraint)."""
    reponse = _appeler(
        cfg,
        [
            {
                "role": "user",
                "content": prompt_plan_semaine(cfg, bilan, debut, adherence, consignes),
            }
        ],
        MAX_TOKENS_PLAN,
        output_format={"type": "json_schema", "schema": SCHEMA_PLAN},
    )

    try:
        payload = json.loads(reponse)
    except json.JSONDecodeError as exc:
        raise CoachError(
            f"Le plan renvoyé n'est pas un JSON valide : {exc}"
        ) from exc

    # Le schéma garantit la forme ; on contrôle le sens et on signale sans rejeter.
    return plan_depuis_payload(payload, debut, bilan)


def question(
    cfg: Config, bilan: Bilan, texte: str, analyse: AnalyseSeance | None = None
) -> str:
    """Répond à une question libre, avec les données d'entraînement en contexte."""
    blocs = [contexte_athlete(cfg), contexte_bilan(bilan)]
    if analyse:
        blocs.append(contexte_seance(analyse))

    prompt = f"""{chr(10).join(blocs)}

---

Question de {cfg.athlete.prenom} : {texte}

Réponds directement, en t'appuyant sur les données ci-dessus quand elles sont
pertinentes. Si la question ne peut pas être tranchée avec ces données, dis
lesquelles manqueraient."""

    return _appeler(cfg, [{"role": "user", "content": prompt}], MAX_TOKENS_TEXTE)
