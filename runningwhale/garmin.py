"""Connexion à Garmin Connect et synchronisation incrémentale des activités."""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from garminconnect import Garmin

from .db import Database
from .models import Activity, activity_from_garmin, laps_from_garmin
from .wellness import Wellness, depuis_garmin

# Marge de recouvrement : on re-télécharge quelques jours déjà connus pour
# rattraper les activités éditées après coup ou synchronisées en retard.
JOURS_RECOUVREMENT = 3

# Les hôtes que la connexion traverse. `sso` et `connect` ne suffisent pas :
# l'échange de jetons OAuth passe par `diauth`, et le parcours mobile par
# `mobile.integration`. Un réseau qui n'autorise qu'une partie de la liste
# échoue tard, sur une erreur d'authentification qui accuse le mot de passe.
HOTES_REQUIS = (
    "sso.garmin.com",
    "connect.garmin.com",
    "connectapi.garmin.com",
    "diauth.garmin.com",
    "mobile.integration.garmin.com",
    "apps.garmin.com",
)


class GarminError(RuntimeError):
    """Erreur de connexion ou d'appel à Garmin Connect."""


# La bibliothèque garminconnect journalise ses échecs avec `logger.exception`,
# ce qui déverse un traceback complet sur la sortie d'erreur avant même qu'on
# ait pu présenter un message utile. On la fait taire le temps de nos appels.
LOGGERS_BAVARDS = ("garminconnect", "garminconnect.client", "garth")


class _Collecteur(logging.Handler):
    """Retient les messages muselés, sans jamais rien écrire sur la sortie."""

    def __init__(self, lignes: list[str], maximum: int = 50) -> None:
        super().__init__(level=logging.DEBUG)
        self.lignes = lignes
        self.maximum = maximum

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.lignes) >= self.maximum:
            return
        try:
            self.lignes.append(record.getMessage()[:500])
        except Exception:  # noqa: BLE001 — un journal ne doit jamais casser l'appel
            pass


@contextmanager
def _silencieux() -> Iterator[list[str]]:
    """Muselle la bibliothèque, mais garde ce qu'elle disait.

    Ces messages sont inutilisables tels quels pour l'athlète, mais ils
    contiennent parfois la seule trace de la vraie cause : la bibliothèque
    rattrape certains échecs (un échange de jetons refusé, par exemple), les
    journalise, puis repart sur une autre voie et échoue plus loin sur une
    exception qui ne dit plus rien de l'incident d'origine. On les collecte
    pour le diagnostic, sans les afficher.
    """
    captures: list[str] = []
    collecteur = _Collecteur(captures)
    etats = {}
    for nom in LOGGERS_BAVARDS:
        journal = logging.getLogger(nom)
        etats[nom] = (journal.level, journal.propagate)
        journal.setLevel(logging.DEBUG)  # tout arrive au collecteur…
        journal.propagate = False  # …et rien ne remonte aux poignées d'origine
        journal.addHandler(collecteur)
    try:
        yield captures
    finally:
        for nom, (niveau, propage) in etats.items():
            journal = logging.getLogger(nom)
            journal.removeHandler(collecteur)
            journal.setLevel(niveau)
            journal.propagate = propage


def masquer_secrets(txt: str) -> str:
    """Efface des sorties tout secret connu (mot de passe, email, clé API).

    Un message d'erreur remonté par la bibliothèque Garmin ou par la pile HTTP
    peut, selon les versions, citer la requête qui a échoué — donc les
    identifiants soumis. On ne contrôle pas ce texte : on le nettoie avant de
    l'afficher ou de le journaliser.
    """
    for var in ("GARMIN_PASSWORD", "ANTHROPIC_API_KEY", "GARMIN_EMAIL"):
        valeur = os.environ.get(var)
        if valeur:
            txt = txt.replace(valeur, f"<{var}>")
    # Clés API Anthropic reconnaissables à leur préfixe, d'où qu'elles viennent.
    txt = re.sub(r"sk-ant-[A-Za-z0-9_-]{8,}", "<ANTHROPIC_API_KEY>", txt)
    return txt


def _chaine_de_causes(exc: BaseException, profondeur_max: int = 8) -> str:
    """Concatène le message d'une exception et de celles qui l'ont causée.

    La bibliothèque enveloppe : un tunnel refusé par un proxy d'entreprise
    ressort en `GarminConnectAuthenticationError` (« profil social
    indisponible »), et la vraie cause n'existe plus que dans `__cause__`.
    """
    messages: list[str] = []
    courante: BaseException | None = exc
    vues: set[int] = set()
    while courante is not None and len(messages) < profondeur_max:
        if id(courante) in vues:
            break
        vues.add(id(courante))
        messages.append(str(courante))
        courante = courante.__cause__ or courante.__context__
    return "\n".join(messages)


# Signatures d'un blocage par un proxy ou une politique de sortie, telles
# qu'elles apparaissent dans la chaîne de causes : CONNECT refusé (curl 56/7),
# 403/407 émis par la passerelle, ou erreur de proxy remontée par la pile HTTP.
_SIGNES_DE_BLOCAGE = (
    "connect tunnel failed",
    "proxyerror",
    "proxy error",
    "tunnel connection failed",
    "407 proxy authentication required",
)


def _hote_bloque(chaine: str) -> str | None:
    """Nomme l'hôte refusé si la chaîne de causes trahit un blocage réseau.

    Renvoie l'hôte quand on peut le lire, une chaîne vide quand le blocage est
    certain mais l'hôte inconnu, et `None` quand rien n'indique un blocage.
    """
    bas = chaine.lower()
    if not any(signe in bas for signe in _SIGNES_DE_BLOCAGE):
        return None
    for hote in HOTES_REQUIS:
        if hote in bas:
            return hote
    return ""


# Signatures d'un refus qui vise l'adresse IP, pas le compte : Garmin filtre
# les plages de centres de données (session cloud, VPS, intégration continue)
# et y répond par une limitation de débit, un CAPTCHA, ou un 403 de Cloudflare
# sur le portail. Aucun de ces trois refus ne parle des identifiants, et aucun
# ne se débloque en corrigeant un mot de passe.
_SIGNES_D_IP_REFUSEE = (
    "ip rate limited",
    "captcha_required",
    "captcha required",
    "portal login failed (non-json): http 403",
)


def _ip_refusee(chaine: str) -> bool:
    """Vrai si la chaîne de causes trahit un refus visant l'IP appelante."""
    bas = chaine.lower()
    return any(signe in bas for signe in _SIGNES_D_IP_REFUSEE)


def _message_ip_refusee() -> str:
    return (
        "Garmin refuse cette adresse IP, pas tes identifiants : la connexion est "
        "partie en limitation de débit puis en CAPTCHA, ce qu'aucun mot de passe "
        "ne corrige. Garmin filtre les IP de centre de données — session cloud, "
        "VPS, intégration continue.\n"
        "La voie qui marche : lance `coach login --exporter` depuis une machine "
        "à IP résidentielle, et pose la valeur imprimée dans le secret "
        "d'environnement GARMIN_TOKENS. Les jetons valent environ un an, et la "
        "synchro repart sans nouvelle authentification."
    )


def _message_de_blocage(hote: str) -> str:
    cible = f"« {hote} »" if hote else "un des hôtes Garmin"
    return (
        f"Accès réseau refusé vers {cible} : un proxy ou une politique de sortie "
        "bloque la connexion — ce n'est pas un problème d'identifiants.\n"
        "La connexion Garmin traverse " + ", ".join(HOTES_REQUIS) + " ; il faut "
        "les autoriser tous. En session Claude Code cloud, c'est la politique "
        "réseau de l'environnement qu'il faut élargir."
    )


def _traduire(
    exc: Exception, action: str, journal: list[str] | None = None
) -> GarminError:
    """Transforme une exception Garmin en message actionnable.

    `journal` est ce que la bibliothèque a dit pendant l'appel (voir
    `_silencieux`) : la cause réelle y figure parfois seule.
    """
    from garminconnect.exceptions import (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    # Avant tout jugement sur les identifiants : un blocage réseau se déguise
    # en échec d'authentification, et envoyer l'athlète vérifier son mot de
    # passe lui ferait chercher au mauvais endroit.
    indices = _chaine_de_causes(exc)
    if journal:
        indices += "\n" + "\n".join(journal)
    hote = _hote_bloque(indices)
    if hote is not None:
        return GarminError(_message_de_blocage(hote))

    # Même piège, autre cause : Garmin refuse l'IP appelante et le dit sous
    # forme de quota, de CAPTCHA ou de 403 Cloudflare — jamais sous forme
    # d'erreur d'identifiants, alors que c'est ainsi que ça ressort en bout de
    # chaîne une fois toutes les voies de connexion épuisées.
    if _ip_refusee(indices):
        return GarminError(_message_ip_refusee())

    if isinstance(exc, GarminConnectAuthenticationError):
        return GarminError(
            "Identifiants refusés par Garmin Connect. Vérifie ton email et ton mot "
            "de passe en te connectant sur connect.garmin.com, puis relance "
            "`coach login`."
        )
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return GarminError(
            "Trop de tentatives de connexion. Garmin limite temporairement l'accès : "
            "attends une quinzaine de minutes avant de réessayer."
        )
    if isinstance(exc, GarminConnectConnectionError):
        return GarminError(
            "Garmin Connect est injoignable. Vérifie ta connexion internet, et "
            "qu'aucun proxy, VPN ou pare-feu ne bloque " + ", ".join(HOTES_REQUIS) + "."
        )

    # Message brut tronqué : inutile de recracher une pile d'appels entière.
    # Et nettoyé : ce texte vient de la bibliothèque, il peut citer la requête
    # qui a échoué, donc les identifiants soumis.
    detail = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
    return GarminError(masquer_secrets(f"{action} : {detail}"))


def _est_bloquante(exc: Exception) -> bool:
    """Vrai pour une erreur qui condamne toute la suite de la synchro.

    Un jeton expiré (401) ou une limitation de débit (429) frapperont chaque
    appel suivant à l'identique : continuer, c'est marteler l'API pour ne rien
    enregistrer, et masquer la vraie cause. La bibliothèque garminconnect
    traduit ces statuts HTTP en exceptions typées sur tous ses points d'API.
    """
    from garminconnect.exceptions import (
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
    )

    return isinstance(
        exc, (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError)
    )


@dataclass
class SyncResult:
    nouvelles: list[Activity]
    mises_a_jour: int
    depuis: date
    ignorees: int = 0  # activités écartées, faute de date exploitable

    @property
    def total_vues(self) -> int:
        return len(self.nouvelles) + self.mises_a_jour


def connect(
    token_dir: Path,
    email: str | None = None,
    password: str | None = None,
    prompt_mfa: Callable[[], str] | None = None,
) -> Garmin:
    """Ouvre une session Garmin, en réutilisant les jetons si possible.

    Les jetons sont stockés dans `token_dir` et durent environ un an, donc
    l'identifiant et le mot de passe ne sont demandés qu'à la première connexion.
    """
    token_dir = Path(token_dir)
    token_dir.mkdir(parents=True, exist_ok=True)
    _restreindre_jetons(token_dir)

    api = Garmin()
    try:
        with _silencieux():
            api.login(_source_des_jetons(token_dir))
        return api
    except Exception:  # jetons absents, invalides ou expirés → connexion complète
        pass

    email = email or os.environ.get("GARMIN_EMAIL")
    password = password or os.environ.get("GARMIN_PASSWORD")
    if not email:
        if not sys.stdin.isatty():
            raise GarminError(
                "Aucun jeton Garmin valide et aucun identifiant fourni. "
                "Lance `coach login` en interactif, ou renseigne GARMIN_EMAIL "
                "et GARMIN_PASSWORD."
            )
        email = input("Email Garmin Connect : ").strip()
    if not password:
        if not sys.stdin.isatty():
            raise GarminError("GARMIN_PASSWORD manquant pour une connexion non interactive.")
        password = getpass.getpass("Mot de passe Garmin Connect : ")

    mfa = prompt_mfa or (lambda: input("Code de vérification (MFA) : ").strip())

    try:
        with _silencieux() as journal:
            api = Garmin(email=email, password=password, prompt_mfa=mfa)
            api.login()
            _sauver_jetons(api, token_dir)
    except Exception as exc:  # noqa: BLE001 — l'API remonte des exceptions variées
        raise _traduire(exc, "connexion à Garmin Connect", journal) from exc

    # La bibliothèque écrit les jetons avec le umask courant (souvent 0644,
    # lisibles par tous) : ils valent un an d'accès au compte, on les restreint
    # aussitôt.
    _restreindre_jetons(token_dir)
    return api


def _source_des_jetons(token_dir: Path) -> str:
    """Où lire les jetons : `GARMIN_TOKENS` s'il est posé, le dossier sinon.

    Une IP de centre de données ne peut pas s'authentifier chez Garmin (voir
    `_message_ip_refusee`), mais elle peut réutiliser des jetons obtenus
    ailleurs. `GARMIN_TOKENS` accepte les deux formes que la bibliothèque sait
    lire : le contenu JSON des jetons, ou un chemin vers le dossier qui les
    contient — ce qui permet de les passer par un secret d'environnement, sans
    jamais les écrire dans le dépôt.
    """
    depuis_env = os.environ.get("GARMIN_TOKENS", "").strip()
    if not depuis_env:
        return str(token_dir)
    # La bibliothèque distingue contenu et chemin sur la longueur (> 512
    # caractères = contenu). Un JSON de jetons plus court serait pris pour un
    # chemin : on l'écrit alors dans le dossier, où il sera lu normalement.
    if depuis_env.startswith("{") and len(depuis_env) <= 512:
        cible = token_dir / "garmin_tokens.json"
        cible.write_text(depuis_env)
        _restreindre_jetons(token_dir)
        return str(token_dir)
    return depuis_env


def _sauver_jetons(api: Garmin, token_dir: Path) -> None:
    """Écrit les jetons de la session, quelle que soit la version installée.

    Le stockage a changé de main : `garminconnect` déléguait à `garth`
    (`api.garth.dump`), les versions récentes gèrent les jetons elles-mêmes
    (`api.client.dump`) et laissent `api.garth` à `None`. Appeler l'ancienne
    forme sur une version récente casse une connexion pourtant réussie, sur une
    `AttributeError` qui ne dit rien de la cause.
    """
    for porteur in (getattr(api, "client", None), getattr(api, "garth", None)):
        sauver = getattr(porteur, "dump", None)
        if callable(sauver):
            sauver(str(token_dir))
            return
    raise GarminError(
        "Connexion réussie, mais cette version de garminconnect n'expose aucun "
        "moyen d'enregistrer les jetons : la prochaine commande devra se "
        "reconnecter. Mets la bibliothèque à jour (`pip install -U garminconnect`)."
    )


def exporter_jetons(api: Garmin) -> str:
    """Sérialise les jetons de la session, prêts à poser dans `GARMIN_TOKENS`.

    C'est le pont entre une machine résidentielle et une session cloud : Garmin
    refuse d'authentifier une IP de centre de données, mais accepte d'y voir
    revivre des jetons obtenus ailleurs (voir `_source_des_jetons`). Même
    prudence de version que `_sauver_jetons` : `client.dumps` sur les versions
    récentes de garminconnect, `garth.dumps` sur les anciennes.
    """
    for porteur in (getattr(api, "client", None), getattr(api, "garth", None)):
        serialiser = getattr(porteur, "dumps", None)
        if callable(serialiser):
            return serialiser()
    raise GarminError(
        "Cette version de garminconnect n'expose aucun moyen de sérialiser "
        "les jetons. Mets la bibliothèque à jour (`pip install -U garminconnect`)."
    )


def _restreindre_jetons(token_dir: Path) -> None:
    """Dossier de jetons en 0700, fichiers de jetons en 0600."""
    try:
        token_dir.chmod(0o700)
        for fichier in token_dir.iterdir():
            if fichier.is_file():
                fichier.chmod(0o600)
    except OSError:  # système de fichiers sans permissions POSIX
        pass


def sync(
    api: Garmin,
    db: Database,
    depuis: date | None = None,
    avec_tours: bool = True,
    verbose: bool = True,
) -> SyncResult:
    """Récupère les activités depuis `depuis` (par défaut : reprise incrémentale)."""
    if depuis is None:
        derniere = db.latest_start()
        if derniere is None:
            depuis = date.today() - timedelta(days=365)
        else:
            depuis = derniere.date() - timedelta(days=JOURS_RECOUVREMENT)

    fin = date.today()
    try:
        with _silencieux() as journal:
            brutes = api.get_activities_by_date(depuis.isoformat(), fin.isoformat())
    except Exception as exc:  # noqa: BLE001
        raise _traduire(exc, "récupération des activités", journal) from exc

    ignorees = 0
    a_traiter: list[Activity] = []
    for data in brutes or []:
        try:
            activity = activity_from_garmin(data)
        except ValueError as exc:
            # Une activité illisible ne doit pas interrompre la synchro, mais
            # elle ne doit pas non plus entrer en base avec une date inventée.
            ignorees += 1
            if verbose:
                print(f"  ! activité {data.get('activityId', '?')} ignorée : {exc}")
            continue
        if not activity.activity_id:
            continue
        a_traiter.append(activity)

    # De la plus ancienne à la plus récente. La reprise incrémentale repart de
    # MAX(debut) en base : si le processus meurt au milieu (coupure, OOM, reboot),
    # il ne doit jamais rester en base une activité plus récente qu'une activité
    # pas encore enregistrée — celle-ci sortirait définitivement de la fenêtre de
    # reprise. Garmin renvoie l'ordre inverse (la plus récente d'abord), donc ce
    # tri est indispensable, pas cosmétique.
    a_traiter.sort(key=lambda a: a.debut)

    nouvelles: list[Activity] = []
    mises_a_jour = 0
    for activity in a_traiter:
        deja_connue = db.get_activity(activity.activity_id) is not None
        # Les tours ne sont téléchargés que pour les courses nouvelles :
        # c'est un appel réseau par activité, inutile de le refaire à chaque synchro.
        if avec_tours and activity.est_course and not deja_connue:
            activity.laps = _fetch_laps(api, activity.activity_id)

        est_nouvelle = db.upsert_activity(activity)
        if est_nouvelle:
            nouvelles.append(activity)
            if verbose:
                print(f"  + {activity.debut:%Y-%m-%d} {activity.nom or activity.type}"
                      f" — {activity.distance_km:.2f} km")
        else:
            mises_a_jour += 1

    if ignorees and verbose:
        print(f"  {ignorees} activité(s) ignorée(s) faute de date exploitable.")

    db.set_meta("derniere_synchro", datetime.now().isoformat(timespec="seconds"))
    return SyncResult(
        nouvelles=nouvelles, mises_a_jour=mises_a_jour, depuis=depuis, ignorees=ignorees
    )


def sync_wellness(
    api: Garmin, db: Database, jours: int = 30, verbose: bool = False
) -> int:
    """Récupère sommeil, VFC, readiness et Body Battery des `jours` derniers jours.

    Chaque journée demande plusieurs appels : on ne re-télécharge donc que ce qui
    manque, plus les deux derniers jours (Garmin consolide certaines métriques
    avec du retard).
    """
    aujourdhui = date.today()
    dernier = db.dernier_jour_wellness()

    debut = aujourdhui - timedelta(days=jours)
    if dernier is not None:
        debut = max(debut, dernier - timedelta(days=2))

    enregistres = 0
    jour = debut
    while jour <= aujourdhui:
        donnees = _fetch_wellness_jour(api, jour)
        if not donnees.est_vide:
            db.upsert_wellness(donnees)
            enregistres += 1
            if verbose:
                details = []
                if donnees.sommeil_h:
                    details.append(f"sommeil {donnees.sommeil_h} h")
                if donnees.vfc_ms:
                    details.append(f"VFC {donnees.vfc_ms:.0f} ms")
                if donnees.readiness_score:
                    details.append(f"readiness {donnees.readiness_score}")
                print(f"  {jour} — {', '.join(details) or 'données partielles'}")
        jour += timedelta(days=1)

    db.set_meta("derniere_synchro_wellness", datetime.now().isoformat(timespec="seconds"))
    return enregistres


def _fetch_wellness_jour(api: Garmin, jour: date) -> Wellness:
    """Interroge les points d'API de récupération pour une journée.

    Chaque appel est isolé : une montre qui ne mesure pas la VFC ou un point
    d'API indisponible ne doit pas faire perdre le reste de la journée.
    """
    iso = jour.isoformat()

    def essayer(appel):
        try:
            with _silencieux() as journal:
                return appel()
        except Exception as exc:  # noqa: BLE001 — donnée absente ou API changée
            # Jeton expiré ou débit limité : chaque jour restant ferait cinq
            # appels voués au même échec, et la synchro se terminerait « sans
            # erreur » en n'ayant rien enregistré. On s'arrête net et on le dit.
            if _est_bloquante(exc):
                raise _traduire(
                    exc, "récupération des données de récupération", journal
                ) from exc
            return None

    return depuis_garmin(
        jour,
        sommeil=essayer(lambda: api.get_sleep_data(iso)),
        vfc=essayer(lambda: api.get_hrv_data(iso)),
        readiness=essayer(lambda: api.get_training_readiness(iso)),
        body_battery=essayer(lambda: api.get_body_battery(iso, iso)),
        stats=essayer(lambda: api.get_stats(iso)),
    )


def _fetch_laps(api: Garmin, activity_id: str) -> list:
    try:
        with _silencieux() as journal:
            splits: dict[str, Any] = api.get_activity_splits(activity_id)
        return laps_from_garmin(splits or {})
    except Exception as exc:  # noqa: BLE001 — les tours sont un bonus, pas un bloquant
        # Sauf si le jeton vient d'expirer en pleine synchro : les tours ne sont
        # téléchargés qu'à la découverte d'une activité, avaler l'erreur les
        # perdrait définitivement. On interrompt la synchro avant d'enregistrer
        # l'activité ; comme on traite du plus ancien au plus récent, la reprise
        # la re-téléchargera, tours compris.
        if _est_bloquante(exc):
            raise _traduire(exc, "récupération des tours", journal) from exc
        return []
