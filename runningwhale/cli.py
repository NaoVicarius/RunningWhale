"""Interface en ligne de commande de RunningWhale."""

from __future__ import annotations

import argparse
import json
import shutil
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import __version__, analysis, composition as composition_mod, llm, plan as plan_mod, report
from .composition import Composition
from .config import Config, default_config_path, ensure_dirs, load_config
from .db import Database
from .garmin import GarminError, connect, masquer_secrets, sync, sync_wellness
from .imports import ImportError_, importer

CLE_PLAN_COURANT = "plan_courant"


# --------------------------------------------------------------------------
# Utilitaires d'affichage
# --------------------------------------------------------------------------

def info(msg: str) -> None:
    print(msg)


def erreur(msg: str) -> None:
    # Les messages d'erreur citent parfois des exceptions de bibliothèques
    # tierces, dont on ne contrôle pas le texte : on efface tout secret connu
    # avant affichage (la sortie peut partir dans un journal, cf. `coach cron`).
    print(f"Erreur : {masquer_secrets(msg)}", file=sys.stderr)


def _contexte(args: argparse.Namespace) -> tuple[Config, Database]:
    cfg = load_config(Path(args.config) if args.config else None)
    ensure_dirs(cfg)
    return cfg, Database(cfg.db_path)


def _bilan(cfg: Config, db: Database, semaines: int = 8) -> analysis.Bilan:
    activites = db.activities()
    if not activites:
        raise SystemExit(
            "Aucune activité en base. Lance `coach sync` pour récupérer tes courses Garmin."
        )
    # Les tours sont nécessaires pour la dérive cardiaque et la répartition
    # d'intensité fine : on les recharge pour les sorties récentes.
    recentes = [a for a in activites if a.debut.date() >= date.today() - timedelta(days=35)]
    detaillees = [db.get_activity(a.activity_id) or a for a in recentes]
    ids_detailles = {a.activity_id for a in detaillees}
    complet = detaillees + [a for a in activites if a.activity_id not in ids_detailles]
    complet.sort(key=lambda a: a.debut, reverse=True)
    return analysis.bilan(
        complet,
        cfg.athlete,
        nb_semaines=semaines,
        wellness=db.wellness(depuis=date.today() - timedelta(days=35)),
        compositions=db.compositions(depuis=date.today() - timedelta(days=90)),
    )


def _plan_courant(db: Database) -> plan_mod.Plan | None:
    brut = db.get_meta(CLE_PLAN_COURANT)
    if not brut:
        return None
    try:
        return plan_mod.Plan.from_json(brut)
    except (ValueError, KeyError, TypeError, AttributeError):
        # Sur une entrée abîmée, `Plan.from_json` lève aussi TypeError (dates
        # d'un mauvais type) et AttributeError (JSON valide mais pas un objet,
        # séances ou étapes qui ne sont pas des dictionnaires). Un plan
        # illisible ne doit jamais casser `coach status` : on fait comme s'il
        # n'y avait pas de plan.
        return None


def _adherence(db: Database) -> plan_mod.Adherence | None:
    courant = _plan_courant(db)
    if courant is None:
        return None
    activites = db.activities(depuis=datetime.combine(courant.debut, datetime.min.time()))
    return plan_mod.rapprocher(courant, activites)


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------

MODE_D_EMPLOI_GARMIN = """Comment récupérer le fichier de tes courses (2 minutes)

  1. Va sur https://connect.garmin.com depuis un ordinateur.
  2. Menu de gauche → « Activités » → « Toutes les activités ».
  3. Fais défiler jusqu'en bas pour charger tout ton historique
     (la page en charge un bout à la fois : plus tu descends, plus tu en auras).
  4. En haut à droite, clique « Exporter CSV ».
  5. Tu obtiens un fichier « Activities.csv » dans tes téléchargements.

Ensuite :  coach import ~/Téléchargements/Activities.csv

Pas de Garmin ? Le coach lit aussi les .tcx, .fit et .gpx, que produisent la
plupart des montres et applications de course."""


def _demander(question: str, defaut: str = "") -> str:
    """Pose une question, renvoie la réponse nettoyée (vide si l'athlète passe)."""
    suffixe = f" [{defaut}]" if defaut else " (entrée pour passer)"
    try:
        reponse = input(f"{question}{suffixe} : ").strip()
    except EOFError:  # entrée non interactive : on garde le défaut
        return defaut
    return reponse or defaut


def _profil_interactif() -> dict[str, Any]:
    """Le strict minimum pour que les calculs tiennent debout.

    Volontairement court : quatre questions, toutes explicables en une ligne.
    Chacune peut être passée — le coach dégrade proprement — mais le poids et
    la taille changent assez les conclusions pour valoir la peine d'insister.
    """
    info("")
    info("Trois minutes de questions, et c'est fini.")
    info("Tu pourras tout modifier plus tard dans le fichier de configuration.")
    info("")

    reponses: dict[str, Any] = {}
    reponses["prenom"] = _demander("Ton prénom", "Athlète")

    info("")
    info("— L'année de naissance sert à estimer ta FC max si tu ne la connais pas.")
    naissance = _demander("Ton année de naissance (ex. 1992)")
    if naissance.isdigit() and 1900 < int(naissance) < date.today().year:
        reponses["naissance"] = f"{naissance}-01"

    info("")
    info("— La taille sert à vérifier qu'on ne te propose pas d'allures")
    info("  plus lentes que ta vitesse de marche (ça arrive vite).")
    taille = _demander("Ta taille (ex. 1m78 ou 178)")
    if taille:
        reponses["taille_cm"] = taille

    info("")
    info("— Le poids sert au coach pour calibrer ses conseils (montée de volume,")
    info("  contraintes articulaires). Il ne sert à aucun calcul de performance.")
    poids = _demander("Ton poids en kg (ex. 74)")
    if poids:
        reponses["poids_kg"] = poids

    info("")
    info("— Blessures passées, gênes récurrentes, articulations fragiles :")
    info("  tout ce que le coach doit éviter de provoquer.")
    contraintes = _demander("Contraintes physiques (une ligne suffit)")
    if contraintes:
        reponses["historique"] = contraintes

    return reponses


def _ecrire_profil(cible: Path, reponses: dict[str, Any]) -> None:
    """Injecte les réponses dans le modèle de configuration, sans le réécrire.

    On part du fichier d'exemple pour garder ses commentaires — c'est lui qui
    explique chaque réglage. Seules les valeurs répondues sont remplacées.
    """
    modele = Path(__file__).parent.parent / "config" / "athlete.example.yml"
    texte = (
        modele.read_text(encoding="utf-8") if modele.exists() else _CONFIG_MINIMALE
    )

    remplacements = {
        "prenom": reponses.get("prenom"),
        "naissance": reponses.get("naissance"),
        "taille_cm": reponses.get("taille_cm"),
        "poids_kg": reponses.get("poids_kg"),
    }
    for cle, valeur in remplacements.items():
        if not valeur:
            continue
        motif = re.compile(rf"^(\s*){cle}:.*$", re.MULTILINE)
        if motif.search(texte):
            texte = motif.sub(rf"\g<1>{cle}: {valeur}", texte, count=1)

    if reponses.get("historique"):
        texte = re.sub(
            r"^(\s*)historique: >\n(?:\s+.*\n)*",
            rf"\g<1>historique: >\n\g<1>  {reponses['historique']}\n",
            texte,
            count=1,
            flags=re.MULTILINE,
        )

    cible.write_text(texte, encoding="utf-8")


def cmd_init(args: argparse.Namespace) -> int:
    cible = Path(args.config) if args.config else default_config_path()
    cible.parent.mkdir(parents=True, exist_ok=True)

    if cible.exists() and not args.force:
        info(f"La configuration existe déjà : {cible}")
        info("Relance avec --force pour l'écraser.")
        return 0

    info("Bienvenue dans RunningWhale 🐋")
    info("")
    info("Un coach de course qui travaille sur TES données, pas sur des moyennes.")

    interactif = sys.stdin.isatty() and not args.sans_questions
    reponses = _profil_interactif() if interactif else {}
    _ecrire_profil(cible, reponses)

    cfg = load_config(cible)
    ensure_dirs(cfg)
    Database(cfg.db_path)

    info("")
    info(f"✓ Profil créé : {cible}")
    info(f"✓ Tes données restent ici, sur ta machine : {cfg.home}")
    info("  Elles ne partent nulle part et ne sont jamais publiées.")
    info("")
    info("─" * 68)
    info(MODE_D_EMPLOI_GARMIN)
    info("─" * 68)
    info("")
    info("Puis :")
    info("  coach status    ton tableau de bord, calculé sur tes vraies sorties")
    info("")
    info("Et si tu as une balance connectée, une pesée aide le coach à ajuster :")
    info("  coach poids --kg 74.2 --graisse 18.5")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    cfg, _ = _contexte(args)
    try:
        api = connect(cfg.token_dir)
    except GarminError as exc:
        erreur(str(exc))
        return 1
    try:
        nom = api.get_full_name()
    except Exception:  # noqa: BLE001
        nom = "(nom indisponible)"
    info(f"Connecté à Garmin Connect : {nom}")
    info(f"Jetons enregistrés dans {cfg.token_dir} — valables environ un an.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg, db = _contexte(args)

    depuis = None
    if args.depuis:
        depuis = datetime.strptime(args.depuis, "%Y-%m-%d").date()
    elif args.jours:
        depuis = date.today() - timedelta(days=args.jours)

    try:
        api = connect(cfg.token_dir)
        info("Synchronisation des activités…")
        resultat = sync(api, db, depuis=depuis, avec_tours=not args.sans_tours)
    except GarminError as exc:
        erreur(str(exc))
        return 1

    info("")
    if resultat.nouvelles:
        info(f"{len(resultat.nouvelles)} nouvelle(s) activité(s) depuis le {resultat.depuis}.")
    else:
        info(f"Aucune nouvelle activité depuis le {resultat.depuis}.")
    info(f"{resultat.mises_a_jour} activité(s) déjà connue(s) rafraîchie(s).")
    info(f"Total en base : {db.count_activities()} activités.")

    if not args.sans_recup:
        info("")
        info("Synchronisation des données de récupération…")
        try:
            jours = sync_wellness(api, db, jours=args.jours_recup, verbose=True)
            info(f"{jours} journée(s) de récupération enregistrée(s).")
        except Exception as exc:  # noqa: BLE001 — la récup ne doit pas casser la synchro
            erreur(f"Récupération indisponible ({exc}). Les activités sont bien à jour.")

    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Importe des activités depuis des fichiers exportés, sans réseau."""
    cfg, db = _contexte(args)

    info(f"Lecture de {args.chemin}…")
    try:
        resultat = importer(
            Path(args.chemin), db, verbose=True, exclure=tuple(args.exclure or ())
        )
    except ImportError_ as exc:
        erreur(str(exc))
        return 1

    info("")
    info(f"{resultat.fichiers_lus} fichier(s) exploitable(s).")
    if resultat.nouvelles:
        info(f"{resultat.nouvelles} nouvelle(s) activité(s) enregistrée(s).")
    else:
        info("Aucune nouvelle activité — tout était déjà en base.")
    if resultat.mises_a_jour:
        info(f"{resultat.mises_a_jour} activité(s) déjà connue(s) rafraîchie(s).")
    if resultat.doublons:
        info(f"{resultat.doublons} séance(s) déjà en base sous un autre export, ignorée(s).")
    if resultat.exclues:
        info(f"{resultat.exclues} activité(s) écartée(s) par --exclure.")

    if resultat.ignorees:
        info("")
        info(f"{len(resultat.ignorees)} entrée(s) écartée(s) :")
        for source, raison in resultat.ignorees[:10]:
            info(f"  ! {source} : {raison}")
        if len(resultat.ignorees) > 10:
            info(f"  … et {len(resultat.ignorees) - 10} autre(s).")

    info("")
    info(f"Total en base : {db.count_activities()} activités.")
    if resultat.nouvelles:
        info("Prochaine étape : `coach status`.")
    return 0


def cmd_poids(args: argparse.Namespace) -> int:
    """Enregistre une pesée, avec ce que la balance a mesuré en plus."""
    cfg, db = _contexte(args)

    jour = date.today()
    if args.date:
        try:
            jour = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            erreur(f"Date illisible : {args.date} (attendu AAAA-MM-JJ).")
            return 1

    imc = args.imc
    if imc is None and cfg.athlete.taille_cm:
        metres = cfg.athlete.taille_cm / 100.0
        imc = args.kg / (metres * metres)

    mesure = Composition(
        jour=jour,
        poids_kg=args.kg,
        imc=imc,
        graisse_pct=args.graisse,
        muscle_squelettique_pct=args.muscle_squelettique,
        masse_hors_graisse_kg=args.hors_graisse,
        gras_sous_cutane_pct=args.sous_cutane,
        graisse_viscerale=args.viscerale,
        eau_pct=args.eau,
        masse_musculaire_kg=args.muscle,
        masse_osseuse_kg=args.os,
        proteines_pct=args.proteines,
        metabolisme_base_kcal=args.metabolisme,
        age_metabolique=args.age_metabolique,
        source=args.source or "",
    )

    nouvelle = db.upsert_composition(mesure)
    info(
        f"Pesée du {jour:%d/%m/%Y} {'enregistrée' if nouvelle else 'mise à jour'} : "
        f"{mesure.poids_kg:.1f} kg"
        + (f", {mesure.graisse_pct:.1f} % de masse grasse" if mesure.graisse_pct else "")
        + "."
    )

    mesures = db.compositions()
    evolution = composition_mod.tendance(mesures)
    if evolution is not None and evolution.delta_kg is not None:
        info(f"Tendance sur 14 jours : {evolution.lecture}.")
    elif len(mesures) < 2:
        info("Une seule pesée en base : la tendance apparaîtra dès la suivante.")

    if cfg.athlete.poids_kg and abs(cfg.athlete.poids_kg - mesure.poids_kg) > 2:
        erreur(
            f"Ton profil annonce {cfg.athlete.poids_kg:.0f} kg, la balance "
            f"{mesure.poids_kg:.1f} kg. Le coach lit le profil : corrige "
            "`poids_kg` dans ta config, sinon ses conseils partiront d'un chiffre faux."
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Tableau de bord local, sans appel au modèle."""
    cfg, db = _contexte(args)
    bilan = _bilan(cfg, db, semaines=args.semaines)

    if args.json:
        donnees = report.bilan_vers_dict(cfg, bilan)
        adherence = _adherence(db)
        if adherence:
            donnees["adherence_plan"] = {
                "prevues": adherence.prevues,
                "realisees": adherence.realisees,
                "taux": adherence.taux,
                "km_prevus": adherence.km_prevus,
                "km_realises": adherence.km_realises,
                "manquees": [
                    {"date": s.date.isoformat(), "titre": s.titre}
                    for s in adherence.manquees
                ],
            }
        print(json.dumps(donnees, ensure_ascii=False, indent=2))
        return 0

    print(report.rapport_bilan(cfg, bilan))

    adherence = _adherence(db)
    if adherence:
        print(f"Plan en cours : {adherence.realisees}/{adherence.prevues} séances réalisées"
              f" ({adherence.taux} %), {adherence.ecart_km:+} km d'écart.")

    derniere_synchro = db.get_meta("derniere_synchro")
    if derniere_synchro:
        print(f"\n_Dernière synchronisation : {derniere_synchro}_")
    return 0


def cmd_bilan(args: argparse.Namespace) -> int:
    cfg, db = _contexte(args)
    bilan = _bilan(cfg, db, semaines=args.semaines)

    commentaire = None
    if not args.sans_ia:
        info("Analyse en cours…")
        try:
            commentaire = llm.bilan_periodique(cfg, bilan)
        except llm.CoachError as exc:
            erreur(str(exc))
            return 1

    contenu = report.rapport_bilan(cfg, bilan, commentaire)
    chemin = report.ecrire(cfg, report.nom_fichier("bilan", date.today()), contenu)
    db.save_report("bilan", contenu, chemin=str(chemin))

    print(contenu)
    info(f"\nRapport enregistré : {chemin}")
    return 0


def cmd_debrief(args: argparse.Namespace) -> int:
    cfg, db = _contexte(args)

    if args.activity_id:
        activite = db.get_activity(args.activity_id)
        if activite is None:
            erreur(f"Activité {args.activity_id} introuvable en base.")
            return 1
    else:
        activite = db.last_activity()
        if activite is None:
            erreur("Aucune course en base. Lance `coach sync`.")
            return 1

    bilan = _bilan(cfg, db)
    analyse = analysis.analyse_seance(activite, cfg.athlete)

    info(f"Débrief de la séance du {activite.debut:%d/%m/%Y}…")
    try:
        commentaire = llm.debrief_seance(cfg, bilan, analyse)
    except llm.CoachError as exc:
        erreur(str(exc))
        return 1

    contenu = report.rapport_debrief(cfg, analyse, bilan, commentaire)
    chemin = report.ecrire(
        cfg,
        report.nom_fichier("debrief", activite.debut, activite.nom or activite.type),
        contenu,
    )
    db.save_report("debrief", contenu, activity_id=activite.activity_id, chemin=str(chemin))

    print(contenu)
    info(f"\nRapport enregistré : {chemin}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    cfg, db = _contexte(args)

    if args.montrer:
        courant = _plan_courant(db)
        if courant is None:
            erreur("Aucun plan enregistré. Lance `coach plan` pour en générer un.")
            return 1
        print(report.rapport_plan(cfg, courant, _adherence(db)))
        return 0

    if args.semaine:
        debut = datetime.strptime(args.semaine, "%Y-%m-%d").date()
    else:
        # Par défaut : le lundi qui vient.
        aujourdhui = date.today()
        debut = aujourdhui + timedelta(days=(7 - aujourdhui.weekday()) % 7 or 7)

    bilan = _bilan(cfg, db)
    adherence = _adherence(db)

    if args.contexte:
        # Tout ce que le modèle recevrait, sans appeler l'API : de quoi produire
        # le plan ailleurs — dans une conversation, par exemple — puis le
        # réinjecter par `--depuis-json`.
        print(llm.prompt_plan_semaine(
            cfg, bilan, debut, adherence=adherence, consignes=args.consignes or ""
        ))
        return 0

    if args.depuis_json:
        chemin_payload = Path(args.depuis_json)
        try:
            payload = json.loads(chemin_payload.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            erreur(f"Plan illisible ({chemin_payload}) : {exc}")
            return 1
        try:
            nouveau = llm.plan_depuis_payload(payload, debut, bilan)
        except (KeyError, TypeError, ValueError) as exc:
            erreur(f"Plan mal formé : {exc}")
            return 1
        info(f"Plan repris depuis {chemin_payload}, sans appel au modèle.")
    else:
        info(f"Construction du plan pour la semaine du {debut:%d/%m/%Y}…")
        try:
            nouveau = llm.plan_semaine(
                cfg, bilan, debut, adherence=adherence, consignes=args.consignes or ""
            )
        except llm.CoachError as exc:
            erreur(str(exc))
            return 1

    contenu = report.rapport_plan(cfg, nouveau, adherence)
    chemin = report.ecrire(cfg, report.nom_fichier("plan", debut), contenu)
    db.save_report("plan", contenu, chemin=str(chemin))
    db.set_meta(CLE_PLAN_COURANT, nouveau.to_json())

    print(contenu)
    info(f"Plan enregistré : {chemin}")

    if args.ics:
        chemin_ics = cfg.reports_dir / f"{debut:%Y-%m-%d}-plan.ics"
        chemin_ics.write_text(plan_mod.to_ics(nouveau), encoding="utf-8")
        info(f"Calendrier exporté : {chemin_ics}")

    if args.push:
        info("Envoi des séances vers Garmin Connect…")
        try:
            api = connect(cfg.token_dir)
        except GarminError as exc:
            erreur(str(exc))
            return 1
        for res in plan_mod.pousser_vers_garmin(api, nouveau):
            if res["erreur"]:
                erreur(f"  {res['date']:%d/%m} {res['seance']} : {res['erreur']}")
            else:
                info(f"  {res['date']:%d/%m} {res['seance']} → séance {res['workout_id']}")

    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    cfg, db = _contexte(args)
    bilan = _bilan(cfg, db)

    analyse = None
    if args.avec_derniere:
        derniere = db.last_activity()
        if derniere:
            analyse = analysis.analyse_seance(derniere, cfg.athlete)

    question = " ".join(args.question)
    try:
        reponse = llm.question(cfg, bilan, question, analyse)
    except llm.CoachError as exc:
        erreur(str(exc))
        return 1

    print(reponse)
    db.save_report("question", f"**{question}**\n\n{reponse}")
    return 0


@contextmanager
def _verrou_exclusif(chemin: Path) -> Iterator[bool]:
    """Verrou de fichier non bloquant. Cède False si un autre le détient.

    Deux `coach watch` qui se chevauchent (cron redéclenché pendant qu'un
    débrief tourne) verraient tous deux la même activité comme « nouvelle » et
    la débrieferaient deux fois — deux appels au modèle, deux rapports. Le
    verrou est libéré par le noyau même si le processus meurt brutalement.
    """
    try:
        import fcntl
    except ImportError:  # plateforme sans flock : on laisse passer
        yield True
        return

    chemin.parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def cmd_watch(args: argparse.Namespace) -> int:
    """Synchronise puis débriefe automatiquement les nouvelles séances.

    C'est la commande à mettre en tâche planifiée.
    """
    cfg, db = _contexte(args)

    with _verrou_exclusif(cfg.home / "watch.lock") as acquis:
        if not acquis:
            info(
                f"[{datetime.now():%Y-%m-%d %H:%M}] Un autre `coach watch` est "
                "déjà en cours, on le laisse finir."
            )
            return 0
        return _watch(cfg, db)


def _watch(cfg: Config, db: Database) -> int:
    try:
        api = connect(cfg.token_dir)
        resultat = sync(api, db, verbose=False)
    except GarminError as exc:
        erreur(str(exc))
        return 1

    nouvelles_courses = [a for a in resultat.nouvelles if a.est_course]
    if not nouvelles_courses:
        info(f"[{datetime.now():%Y-%m-%d %H:%M}] Aucune nouvelle course.")
        return 0

    info(f"[{datetime.now():%Y-%m-%d %H:%M}] {len(nouvelles_courses)} nouvelle(s) course(s).")
    bilan = _bilan(cfg, db)

    for activite in nouvelles_courses:
        if db.has_report("debrief", activite.activity_id):
            continue
        complete = db.get_activity(activite.activity_id) or activite
        analyse = analysis.analyse_seance(complete, cfg.athlete)
        try:
            commentaire = llm.debrief_seance(cfg, bilan, analyse)
        except llm.CoachError as exc:
            erreur(f"Débrief impossible pour {activite.activity_id} : {exc}")
            continue

        contenu = report.rapport_debrief(cfg, analyse, bilan, commentaire)
        chemin = report.ecrire(
            cfg,
            report.nom_fichier("debrief", complete.debut, complete.nom or complete.type),
            contenu,
        )
        db.save_report(
            "debrief", contenu, activity_id=complete.activity_id, chemin=str(chemin)
        )
        info(f"  Débrief écrit : {chemin}")

    return 0


def cmd_cron(args: argparse.Namespace) -> int:
    """Affiche la ligne de crontab à installer.

    La clé API est lue depuis un fichier d'environnement restreint (0600),
    comme le fait l'unité systemd de `scripts/` avec `EnvironmentFile`.
    L'écrire en clair dans la crontab l'exposerait à `crontab -l` et, le temps
    de l'exécution, à `ps` ; et `$ANTHROPIC_API_KEY` ne se résoudrait pas :
    cron lance les commandes dans un environnement quasi vide.
    """
    cfg, _ = _contexte(args)
    executable = shutil.which("coach") or f"{sys.executable} -m runningwhale.cli"
    fichier_env = cfg.home / "env"
    ligne = (
        f"{args.minute} {args.heure} * * * "
        f". {fichier_env} && {executable} watch "
        f">> {cfg.home / 'watch.log'} 2>&1"
    )
    info("1. Mets ta clé API dans un fichier lisible par toi seul :")
    info("")
    info(f"     printf 'ANTHROPIC_API_KEY=%s\\n' \"ta-clé\" > {fichier_env}")
    info(f"     chmod 600 {fichier_env}")
    info("")
    info("2. Ajoute cette ligne à ta crontab (`crontab -e`) :")
    info("")
    info(f"  {ligne}")
    info("")
    info("Elle synchronise Garmin et débriefe automatiquement toute nouvelle séance.")
    info("Ne mets jamais la clé en clair dans la crontab : elle serait lisible")
    info("via `crontab -l` et apparaîtrait dans `ps` pendant l'exécution.")
    info(f"Journal : {cfg.home / 'watch.log'}")
    return 0


_CONFIG_MINIMALE = """athlete:
  prenom: Athlète
objectifs: []
"""


# --------------------------------------------------------------------------
# Analyseur d'arguments
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coach",
        description="Coach de running personnel adossé à tes données Garmin Connect.",
    )
    parser.add_argument("--version", action="version", version=f"RunningWhale {__version__}")
    parser.add_argument("--config", help="Chemin du fichier de configuration YAML")

    sous = parser.add_subparsers(dest="commande", required=True)

    p = sous.add_parser("init", help="Créer ton profil et tes dossiers de travail")
    p.add_argument("--force", action="store_true", help="Écraser une config existante")
    p.add_argument("--sans-questions", action="store_true",
                   help="Ne rien demander, écrire le profil d'exemple à compléter")
    p.set_defaults(func=cmd_init)

    p = sous.add_parser("login", help="Se connecter à Garmin Connect")
    p.set_defaults(func=cmd_login)

    p = sous.add_parser("sync", help="Récupérer les nouvelles activités Garmin")
    p.add_argument("--depuis", help="Date de départ (AAAA-MM-JJ)")
    p.add_argument("--jours", type=int, help="Nombre de jours à remonter")
    p.add_argument("--sans-tours", action="store_true",
                   help="Ne pas télécharger le détail des tours")
    p.add_argument("--sans-recup", action="store_true",
                   help="Ne pas synchroniser sommeil, VFC et readiness")
    p.add_argument("--jours-recup", type=int, default=30,
                   help="Profondeur de la synchro récupération (défaut : 30 jours)")
    p.set_defaults(func=cmd_sync)

    p = sous.add_parser(
        "import",
        help="Importer des activités exportées (zip du compte, TCX, FIT, GPX)",
    )
    p.add_argument(
        "chemin",
        help="Fichier, archive ou dossier à lire (récursif)",
    )
    p.add_argument(
        "--exclure",
        action="append",
        metavar="TYPE",
        help="Écarter un type d'activité (répétable), ex. --exclure musculation",
    )
    p.set_defaults(func=cmd_import)

    p = sous.add_parser("poids", help="Enregistrer une pesée (balance connectée ou non)")
    p.add_argument("--kg", type=float, required=True, help="Poids en kilogrammes")
    p.add_argument("--date", help="Jour de la pesée (AAAA-MM-JJ, défaut : aujourd'hui)")
    p.add_argument("--graisse", type=float, help="Masse grasse (%%)")
    p.add_argument("--muscle-squelettique", type=float, help="Muscle squelettique (%%)")
    p.add_argument("--hors-graisse", type=float, help="Poids hors masse grasse (kg)")
    p.add_argument("--sous-cutane", type=float, help="Gras sous-cutané (%%)")
    p.add_argument("--viscerale", type=float, help="Indice de graisse viscérale")
    p.add_argument("--eau", type=float, help="Eau corporelle totale (%%)")
    p.add_argument("--muscle", type=float, help="Masse musculaire (kg)")
    p.add_argument("--os", type=float, help="Masse osseuse (kg)")
    p.add_argument("--proteines", type=float, help="Protéines (%%)")
    p.add_argument("--metabolisme", type=int, help="Métabolisme de base (kcal/j)")
    p.add_argument("--age-metabolique", type=int, help="Âge métabolique")
    p.add_argument("--imc", type=float, help="IMC (calculé depuis la taille si absent)")
    p.add_argument("--source", help="Appareil, ex. « balance Renpho »")
    p.set_defaults(func=cmd_poids)

    p = sous.add_parser("status", help="Tableau de bord local (sans appel au modèle)")
    p.add_argument("--semaines", type=int, default=8, help="Nombre de semaines affichées")
    p.add_argument("--json", action="store_true",
                   help="Sortie JSON, pour un script ou un agent")
    p.set_defaults(func=cmd_status)

    p = sous.add_parser("bilan", help="Bilan complet analysé par le coach")
    p.add_argument("--semaines", type=int, default=8)
    p.add_argument("--sans-ia", action="store_true", help="Chiffres seuls, sans analyse")
    p.set_defaults(func=cmd_bilan)

    p = sous.add_parser("debrief", help="Débriefer une séance (la dernière par défaut)")
    p.add_argument("activity_id", nargs="?", help="Identifiant Garmin de l'activité")
    p.set_defaults(func=cmd_debrief)

    p = sous.add_parser("plan", help="Générer le plan de la semaine")
    p.add_argument("--semaine", help="Lundi de la semaine visée (AAAA-MM-JJ)")
    p.add_argument("--consignes", help="Contrainte particulière à prendre en compte")
    p.add_argument("--ics", action="store_true", help="Exporter aussi un calendrier .ics")
    p.add_argument("--push", action="store_true",
                   help="Téléverser les séances dans Garmin Connect")
    p.add_argument("--montrer", action="store_true",
                   help="Afficher le plan en cours sans en générer un nouveau")
    p.add_argument("--contexte", action="store_true",
                   help="Afficher la consigne complète sans appeler le modèle")
    p.add_argument("--depuis-json", metavar="FICHIER",
                   help="Reprendre un plan déjà produit (même contrôles, sans appel)")
    p.set_defaults(func=cmd_plan)

    p = sous.add_parser("ask", help="Poser une question au coach")
    p.add_argument("question", nargs="+", help="La question")
    p.add_argument("--avec-derniere", action="store_true",
                   help="Inclure le détail de la dernière séance dans le contexte")
    p.set_defaults(func=cmd_ask)

    p = sous.add_parser("watch", help="Synchroniser et débriefer les nouvelles séances")
    p.set_defaults(func=cmd_watch)

    p = sous.add_parser("cron", help="Afficher la ligne de crontab à installer")
    p.add_argument("--heure", default="21", help="Heure d'exécution (défaut : 21)")
    p.add_argument("--minute", default="30", help="Minute d'exécution (défaut : 30)")
    p.set_defaults(func=cmd_cron)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            erreur(exc.code)
            return 1
        raise
    except KeyboardInterrupt:
        erreur("Interrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
