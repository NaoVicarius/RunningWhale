"""Tests de la couche Garmin : traduction des erreurs et synchronisation.

Aucun appel réseau : l'API Garmin est simulée. Ces tests portent sur ce que
l'utilisateur voit quand ça se passe mal, et sur la logique de reprise
incrémentale.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from runningwhale.db import Database
from runningwhale.garmin import (
    GarminError,
    _chaine_de_causes,
    _sauver_jetons,
    _silencieux,
    _source_des_jetons,
    _traduire,
    exporter_jetons,
    sync,
    sync_wellness,
)
from runningwhale.models import Activity


# ---- traduction des erreurs ----

def test_erreur_authentification_est_actionnable():
    msg = str(_traduire(GarminConnectAuthenticationError("401"), "connexion"))
    assert "Identifiants refusés" in msg
    assert "coach login" in msg
    # Pas de jargon technique ni de trace d'exception
    assert "401" not in msg


def test_erreur_reseau_pointe_vers_le_reseau():
    msg = str(_traduire(GarminConnectConnectionError("proxy 403"), "connexion"))
    assert "injoignable" in msg
    assert "sso.garmin.com" in msg


def test_blocage_reseau_deguise_en_echec_d_authentification():
    # Cas réel en session cloud : le tunnel vers diauth.garmin.com est refusé
    # par la politique de sortie, et garminconnect le rapporte en fin de course
    # comme une erreur d'authentification. Accuser le mot de passe enverrait
    # chercher au mauvais endroit.
    cause = RuntimeError(
        "Failed to perform, curl: (7) CONNECT tunnel failed, response 403 "
        "for https://diauth.garmin.com/di-oauth/exchange"
    )
    exc = GarminConnectAuthenticationError("Failed to retrieve social profile")
    exc.__cause__ = cause

    msg = str(_traduire(exc, "connexion à Garmin Connect"))
    assert "diauth.garmin.com" in msg
    assert "Identifiants refusés" not in msg
    assert "politique" in msg.lower()


def test_blocage_reseau_sans_hote_identifiable_reste_explicite():
    exc = GarminConnectConnectionError("ProxyError: tunnel connection failed")
    msg = str(_traduire(exc, "connexion"))
    assert "refusé" in msg.lower()
    # La liste complète des hôtes à autoriser est donnée, faute de coupable nommé
    assert "diauth.garmin.com" in msg
    assert "mobile.integration.garmin.com" in msg


def test_ip_de_centre_de_donnees_refusee_n_accuse_pas_le_mot_de_passe():
    # Cas réel en session cloud, une fois les hôtes Garmin tous joignables :
    # Garmin refuse l'IP (quota puis CAPTCHA) et l'échec ressort en bout de
    # chaîne comme un problème d'authentification. Envoyer l'athlète changer
    # son mot de passe ne débloquerait rien.
    journal = [
        "mobile+requests returned 429: Mobile login returned 429 — IP rate limited by Garmin",
        "portal+cffi(safari) failed: Portal web login failed: "
        "{'responseStatus': {'type': 'CAPTCHA_REQUIRED', 'message': ''}}",
    ]
    exc = GarminConnectAuthenticationError("All login strategies exhausted")

    msg = str(_traduire(exc, "connexion à Garmin Connect", journal))
    assert "Identifiants refusés" not in msg
    assert "adresse IP" in msg
    # La seule voie de sortie réelle est nommée
    assert "GARMIN_TOKENS" in msg


def test_403_cloudflare_du_portail_est_lu_comme_un_refus_d_ip():
    exc = GarminConnectConnectionError(
        "All login strategies exhausted: Portal login failed (non-JSON): HTTP 403"
    )
    msg = str(_traduire(exc, "connexion"))
    assert "adresse IP" in msg
    assert "injoignable" not in msg


def test_un_403_ordinaire_ne_declenche_pas_le_diagnostic_d_ip():
    # Le motif est spécifique au portail : un 403 quelconque doit garder sa
    # traduction réseau, sinon on remplace un faux diagnostic par un autre.
    msg = str(_traduire(GarminConnectConnectionError("HTTP 403"), "connexion"))
    assert "injoignable" in msg


# ---- jetons ----

def test_jetons_injectes_par_l_environnement_sont_preferes(tmp_path, monkeypatch):
    # Une IP refusée par Garmin ne peut pas s'authentifier, mais elle peut
    # réutiliser des jetons obtenus ailleurs.
    monkeypatch.setenv("GARMIN_TOKENS", "x" * 600)
    assert _source_des_jetons(tmp_path) == "x" * 600


def test_jetons_json_courts_sont_ecrits_sur_disque(tmp_path, monkeypatch):
    # La bibliothèque distingue contenu et chemin sur la longueur : un JSON
    # court passerait pour un chemin et ne serait jamais lu.
    monkeypatch.setenv("GARMIN_TOKENS", '{"di_token": "abc"}')
    assert _source_des_jetons(tmp_path) == str(tmp_path)
    assert (tmp_path / "garmin_tokens.json").read_text() == '{"di_token": "abc"}'


def test_sans_variable_les_jetons_viennent_du_dossier(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_TOKENS", raising=False)
    assert _source_des_jetons(tmp_path) == str(tmp_path)


def test_sauvegarde_des_jetons_suit_la_version_de_la_bibliotheque(tmp_path):
    # Les versions récentes gèrent les jetons elles-mêmes et laissent
    # `api.garth` à None : appeler l'ancienne forme casserait une connexion
    # pourtant réussie.
    class Client:
        def __init__(self):
            self.chemin = None

        def dump(self, path):
            self.chemin = path

    class ApiRecente:
        garth = None

        def __init__(self):
            self.client = Client()

    api = ApiRecente()
    _sauver_jetons(api, tmp_path)
    assert api.client.chemin == str(tmp_path)


def test_sauvegarde_des_jetons_retombe_sur_garth(tmp_path):
    class Garth:
        def __init__(self):
            self.chemin = None

        def dump(self, path):
            self.chemin = path

    class ApiAncienne:
        client = None

        def __init__(self):
            self.garth = Garth()

    api = ApiAncienne()
    _sauver_jetons(api, tmp_path)
    assert api.garth.chemin == str(tmp_path)


def test_sauvegarde_impossible_le_dit_sans_pretendre_avoir_reussi(tmp_path):
    class ApiMuette:
        client = None
        garth = None

    with pytest.raises(GarminError) as err:
        _sauver_jetons(ApiMuette(), tmp_path)
    assert "jetons" in str(err.value)


def test_export_des_jetons_prefere_le_client_recent():
    class Client:
        def dumps(self):
            return '{"di_token": "abc"}'

    class ApiRecente:
        garth = None
        client = Client()

    assert exporter_jetons(ApiRecente()) == '{"di_token": "abc"}'


def test_export_des_jetons_retombe_sur_garth():
    class Garth:
        def dumps(self):
            return "blob-base64"

    class ApiAncienne:
        client = None
        garth = Garth()

    assert exporter_jetons(ApiAncienne()) == "blob-base64"


def test_export_impossible_le_dit(tmp_path):
    class ApiMuette:
        client = None
        garth = None

    with pytest.raises(GarminError) as err:
        exporter_jetons(ApiMuette())
    assert "sérialiser" in str(err.value)


def test_chaine_de_causes_supporte_un_cycle():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert "a" in _chaine_de_causes(a) and "b" in _chaine_de_causes(a)


def test_erreur_de_quota_conseille_d_attendre():
    msg = str(_traduire(GarminConnectTooManyRequestsError("429"), "connexion"))
    assert "Trop de tentatives" in msg
    assert "attends" in msg.lower()


def test_erreur_inconnue_est_tronquee():
    # Une exception bavarde ne doit pas déverser sa pile dans le terminal
    bavarde = RuntimeError("première ligne\n" + "x" * 500)
    msg = str(_traduire(bavarde, "récupération des activités"))
    assert msg.startswith("récupération des activités : première ligne")
    assert len(msg) < 300


def test_erreur_sans_message_utilise_le_type():
    assert "ValueError" in str(_traduire(ValueError(), "connexion"))


def test_toutes_les_traductions_sont_des_garmin_error():
    for exc in (
        GarminConnectAuthenticationError("x"),
        GarminConnectConnectionError("x"),
        RuntimeError("x"),
    ):
        assert isinstance(_traduire(exc, "action"), GarminError)


# ---- silence des journaux ----

def test_silencieux_museler_puis_restaurer():
    journal = logging.getLogger("garminconnect")
    journal.setLevel(logging.DEBUG)

    with _silencieux():
        # Rien ne remonte aux poignées d'origine : la sortie reste propre
        assert journal.propagate is False

    assert journal.level == logging.DEBUG
    assert journal.propagate is True
    assert not journal.handlers  # le collecteur est retiré en sortant


def test_silencieux_collecte_ce_qu_il_muselle():
    journal = logging.getLogger("garminconnect")

    with _silencieux() as captures:
        journal.warning("DI token exchange failed (%s)", "CONNECT tunnel failed, 403")

    assert any("CONNECT tunnel failed" in ligne for ligne in captures)


def test_journal_capture_revele_le_blocage_que_l_exception_a_perdu():
    # La bibliothèque rattrape l'échange de jetons refusé, le journalise, puis
    # repart sur une autre voie et échoue sur un 403 qui ne dit plus rien.
    journal = [
        "DI token exchange failed (Failed to perform, curl: (7) CONNECT tunnel "
        "failed, response 403 for https://diauth.garmin.com/), falling back to JWT_WEB"
    ]
    exc = GarminConnectAuthenticationError("Failed to retrieve social profile")

    msg = str(_traduire(exc, "connexion à Garmin Connect", journal))
    assert "diauth.garmin.com" in msg
    assert "Identifiants refusés" not in msg


def test_silencieux_restaure_meme_en_cas_d_erreur():
    journal = logging.getLogger("garminconnect")
    journal.setLevel(logging.INFO)

    with pytest.raises(ValueError):
        with _silencieux():
            raise ValueError("boum")

    assert journal.level == logging.INFO


# ---- synchronisation ----

class FausseApi:
    """API Garmin simulée."""

    def __init__(self, activites: list[dict] | None = None, erreur: Exception | None = None):
        self._activites = activites or []
        self._erreur = erreur
        self.appels_splits: list[str] = []

    def get_activities_by_date(self, debut, fin, *args, **kwargs):
        if self._erreur:
            raise self._erreur
        return self._activites

    def get_activity_splits(self, activity_id):
        self.appels_splits.append(activity_id)
        return {
            "lapDTOs": [
                {"distance": 1000, "duration": 300, "averageHR": 150, "elevationGain": 5}
            ]
        }


def activite_garmin(aid: str, jours_avant: int = 0, km: float = 10.0) -> dict:
    debut = datetime.now() - timedelta(days=jours_avant)
    return {
        "activityId": aid,
        "startTimeLocal": debut.strftime("%Y-%m-%d %H:%M:%S"),
        "activityType": {"typeKey": "running"},
        "activityName": f"Sortie {aid}",
        "distance": km * 1000,
        "duration": km * 300,
        "movingDuration": km * 300,
        "averageHR": 150,
    }


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite")


def test_sync_enregistre_les_nouvelles_activites(db):
    api = FausseApi([activite_garmin("1"), activite_garmin("2", jours_avant=2)])
    res = sync(api, db, verbose=False)

    assert len(res.nouvelles) == 2
    assert res.mises_a_jour == 0
    assert db.count_activities() == 2


def test_sync_est_idempotente(db):
    api = FausseApi([activite_garmin("1")])
    sync(api, db, verbose=False)
    res = sync(api, db, verbose=False)

    assert len(res.nouvelles) == 0
    assert res.mises_a_jour == 1
    assert db.count_activities() == 1


def test_sync_telecharge_les_tours_une_seule_fois(db):
    api = FausseApi([activite_garmin("1")])
    sync(api, db, verbose=False)
    assert api.appels_splits == ["1"]

    # Deuxième passage : l'activité est connue, inutile de re-télécharger
    sync(api, db, verbose=False)
    assert api.appels_splits == ["1"]

    assert len(db.get_activity("1").laps) == 1


def test_sync_reprend_avec_une_marge_de_recouvrement(db):
    db.upsert_activity(
        Activity(
            activity_id="ancienne",
            debut=datetime.now() - timedelta(days=10),
            type="running",
            distance_m=10000,
            duree_s=3000,
        )
    )
    res = sync(FausseApi([]), db, verbose=False)

    # On repart 3 jours avant la dernière activité connue, pas depuis celle-ci
    assert res.depuis == (date.today() - timedelta(days=13))


def test_sync_sans_historique_remonte_un_an(db):
    res = sync(FausseApi([]), db, verbose=False)
    assert res.depuis == date.today() - timedelta(days=365)


def test_sync_ignore_les_activites_sans_identifiant(db):
    api = FausseApi([{"startTimeLocal": "2026-08-14 10:00:00"}])
    assert len(sync(api, db, verbose=False).nouvelles) == 0


def test_sync_traduit_les_erreurs(db):
    api = FausseApi(erreur=GarminConnectConnectionError("proxy"))
    with pytest.raises(GarminError, match="injoignable"):
        sync(api, db, verbose=False)


def test_sync_enregistre_l_horodatage(db):
    sync(FausseApi([]), db, verbose=False)
    assert db.get_meta("derniere_synchro") is not None


# ---- synchronisation de la récupération ----

class ApiWellness:
    """API simulée pour les données de récupération."""

    def __init__(self, casse: set[str] | None = None):
        self.casse = casse or set()
        self.jours_demandes: list[str] = []

    def _peut(self, nom: str):
        if nom in self.casse:
            raise RuntimeError(f"{nom} indisponible")

    def get_sleep_data(self, cdate):
        self._peut("sommeil")
        self.jours_demandes.append(cdate)
        return {"dailySleepDTO": {"sleepTimeSeconds": 27000}, "restingHeartRate": 48}

    def get_hrv_data(self, cdate):
        self._peut("vfc")
        return {"hrvSummary": {"lastNightAvg": 60, "baseline": {"balancedLow": 52}}}

    def get_training_readiness(self, cdate):
        self._peut("readiness")
        return [{"score": 70, "level": "HIGH"}]

    def get_body_battery(self, debut, fin):
        self._peut("bb")
        return [{"bodyBatteryValuesArray": [[0, 30], [1, 90]]}]

    def get_stats(self, cdate):
        self._peut("stats")
        return {"restingHeartRate": 48, "averageStressLevel": 25}


def test_sync_wellness_enregistre_les_journees(db):
    n = sync_wellness(ApiWellness(), db, jours=5)
    assert n == 6  # les 5 derniers jours plus aujourd'hui
    assert db.dernier_jour_wellness() == date.today()


def test_sync_wellness_survit_a_un_point_d_api_absent(db):
    # Une montre sans capteur de VFC ni readiness ne doit pas tout faire échouer
    sync_wellness(ApiWellness(casse={"vfc", "readiness"}), db, jours=2)
    jour = db.wellness_du_jour(date.today())
    assert jour is not None
    assert jour.sommeil_h == 7.5
    assert jour.vfc_ms is None


def test_sync_wellness_n_enregistre_pas_les_journees_vides(db):
    api = ApiWellness(casse={"sommeil", "vfc", "readiness", "bb", "stats"})
    assert sync_wellness(api, db, jours=3) == 0


def test_sync_wellness_reprend_ou_il_s_est_arrete(db):
    sync_wellness(ApiWellness(), db, jours=10)
    api = ApiWellness()
    sync_wellness(api, db, jours=10)

    # Reprise : seulement les deux derniers jours re-vérifiés, plus aujourd'hui
    assert len(api.jours_demandes) <= 3


# ---- régressions trouvées à l'audit du 2026-08-16 ----

class ApiDatesCassees:
    """API renvoyant une activité dont la date est illisible."""

    def get_activities_by_date(self, debut, fin, *args, **kwargs):
        return [
            activite_garmin("bonne"),
            {**activite_garmin("cassee"), "startTimeLocal": "14/08/2026"},
        ]

    def get_activity_splits(self, activity_id):
        return {"lapDTOs": []}


def test_activite_sans_date_exploitable_est_ecartee(db):
    # Retomber sur 1970 ferait entrer l'activité en base et fausserait
    # silencieusement toutes les fenêtres de calcul.
    res = sync(ApiDatesCassees(), db, verbose=False)

    assert len(res.nouvelles) == 1
    assert res.nouvelles[0].activity_id == "bonne"
    assert res.ignorees == 1
    assert db.get_activity("cassee") is None


def test_une_activite_illisible_n_interrompt_pas_la_synchro(db):
    # L'activité fautive est la seconde : la première doit tout de même passer.
    assert sync(ApiDatesCassees(), db, verbose=False).total_vues == 1


# ---- régressions trouvées à l'audit du 2026-08-16 (modes de défaillance) ----

class DbQuiMeurt(Database):
    """Simule un crash (coupure, OOM-kill) après N upserts."""

    def __init__(self, path, survivre: int):
        super().__init__(path)
        self.survivre = survivre

    def upsert_activity(self, activity):
        if self.survivre <= 0:
            raise KeyboardInterrupt("crash simulé")
        self.survivre -= 1
        return super().upsert_activity(activity)


def test_synchro_interrompue_ne_perd_pas_les_anciennes_activites(tmp_path):
    """Garmin renvoie la plus récente d'abord. Si elle entrait en base la
    première et que le processus mourait là, la reprise (MAX(debut) - 3 j) ne
    re-téléchargerait jamais les plus anciennes : perte définitive."""
    api = FausseApi([activite_garmin("recente"), activite_garmin("ancienne", jours_avant=30)])

    db = DbQuiMeurt(tmp_path / "crash.sqlite", survivre=1)
    with pytest.raises(KeyboardInterrupt):
        sync(api, db, verbose=False)

    # C'est la plus ancienne qui doit avoir été enregistrée en premier…
    reprise = Database(tmp_path / "crash.sqlite")
    assert [a.activity_id for a in reprise.activities()] == ["ancienne"]

    # …pour que la reprise incrémentale retrouve tout le reste.
    api_reprise = FausseApi([activite_garmin("recente")])
    sync(api_reprise, reprise, verbose=False)
    assert {a.activity_id for a in reprise.activities()} == {"ancienne", "recente"}


class ApiJetonExpire(ApiWellness):
    """Le jeton expire en pleine synchro : chaque appel renvoie un 401."""

    def get_sleep_data(self, cdate):
        raise GarminConnectAuthenticationError("401")


def test_jeton_expire_en_pleine_synchro_wellness_interrompt_et_previent(db):
    # Avaler le 401 ferait 5 appels par jour voués au même échec, et une
    # synchro « réussie » qui n'a rien enregistré. On veut une erreur nette.
    with pytest.raises(GarminError, match="Identifiants refusés"):
        sync_wellness(ApiJetonExpire(), db, jours=5)
    assert db.dernier_jour_wellness() is None


class ApiJetonExpirePendantLesTours(FausseApi):
    """Le jeton expire entre la liste des activités et le détail des tours."""

    def get_activity_splits(self, activity_id):
        self.appels_splits.append(activity_id)
        if activity_id == "recente":
            raise GarminConnectAuthenticationError("401")
        return super().get_activity_splits(activity_id)


def test_jeton_expire_pendant_les_tours_n_enregistre_pas_l_activite(db):
    """Les tours ne sont téléchargés qu'à la découverte d'une activité :
    enregistrer l'activité sans eux les perdrait pour toujours. La synchro
    s'arrête avant l'upsert, la reprise re-traitera l'activité entière."""
    api = ApiJetonExpirePendantLesTours(
        [activite_garmin("recente"), activite_garmin("ancienne", jours_avant=5)]
    )
    with pytest.raises(GarminError, match="Identifiants refusés"):
        sync(api, db, verbose=False)

    # L'ancienne (traitée d'abord) est là avec ses tours ; la récente attendra.
    assert db.get_activity("recente") is None
    assert len(db.get_activity("ancienne").laps) == 1


def test_erreur_passagere_sur_les_tours_donne_une_activite_sans_tours(db):
    class ApiToursCasses(FausseApi):
        def get_activity_splits(self, activity_id):
            raise RuntimeError("endpoint en carafe")

    res = sync(ApiToursCasses([activite_garmin("1")]), db, verbose=False)
    assert len(res.nouvelles) == 1
    assert db.get_activity("1").laps == []
