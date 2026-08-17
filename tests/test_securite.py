"""Tests de sécurité et de vie privée : permissions, secrets, élagage, bornes.

Chaque test correspond à un constat démontré de l'audit sécurité — aucun
n'appelle le réseau.
"""

from __future__ import annotations

import os
import stat
from datetime import date, datetime, timedelta

import pytest

from runningwhale import cli, garmin, report
from runningwhale.config import Athlete, Config, ensure_dirs
from runningwhale.db import Database
from runningwhale.garmin import _restreindre_jetons, _traduire, masquer_secrets
from runningwhale.models import activity_from_garmin


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _cfg(tmp_path) -> Config:
    return Config(
        athlete=Athlete(prenom="Alex"),
        home=tmp_path / "home",
        db_path=tmp_path / "home" / "runningwhale.db",
        reports_dir=tmp_path / "home" / "reports",
        token_dir=tmp_path / "home" / "garmin_tokens",
        config_path=tmp_path / "athlete.yml",
    )


# ---- permissions disque ----

def test_ensure_dirs_restreint_a_l_utilisateur(tmp_path):
    ancien = os.umask(0o022)  # umask permissif classique
    try:
        cfg = _cfg(tmp_path)
        ensure_dirs(cfg)
    finally:
        os.umask(ancien)
    assert _mode(cfg.home) == 0o700
    assert _mode(cfg.reports_dir) == 0o700
    assert _mode(cfg.token_dir) == 0o700


def test_ensure_dirs_resserre_un_dossier_existant(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.home.mkdir(parents=True)
    cfg.home.chmod(0o755)  # créé par une version antérieure
    ensure_dirs(cfg)
    assert _mode(cfg.home) == 0o700


def test_base_sqlite_lisible_par_l_utilisateur_seul(tmp_path):
    ancien = os.umask(0o022)
    try:
        db_path = tmp_path / "runningwhale.db"
        Database(db_path)
    finally:
        os.umask(ancien)
    assert _mode(db_path) == 0o600


def test_jetons_garmin_restreints(tmp_path):
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    token_dir.chmod(0o755)
    fichier = token_dir / "oauth1_token.json"
    fichier.write_text("{}")
    fichier.chmod(0o644)  # ce que garth écrit avec le umask par défaut

    _restreindre_jetons(token_dir)

    assert _mode(token_dir) == 0o700
    assert _mode(fichier) == 0o600


def test_connect_resserre_les_jetons_au_passage(tmp_path, monkeypatch):
    """Une connexion par jetons existants resserre un dossier trop ouvert."""

    class FauxGarmin:
        def login(self, tokendir=None):
            return True

    monkeypatch.setattr(garmin, "Garmin", FauxGarmin)
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    token_dir.chmod(0o755)
    (token_dir / "oauth2_token.json").write_text("{}")
    (token_dir / "oauth2_token.json").chmod(0o644)

    garmin.connect(token_dir)

    assert _mode(token_dir) == 0o700
    assert _mode(token_dir / "oauth2_token.json") == 0o600


# ---- fuite de secrets dans les sorties ----

def test_traduire_masque_le_mot_de_passe(monkeypatch):
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2secret")
    exc = RuntimeError("POST failed body=username=alex&password=hunter2secret")
    msg = str(_traduire(exc, "connexion"))
    assert "hunter2secret" not in msg
    assert "<GARMIN_PASSWORD>" in msg


def test_masquer_secrets_reconnait_une_cle_anthropic():
    txt = "Authorization: Bearer sk-ant-api03-abcdefghijkl refusé"
    assert "sk-ant" not in masquer_secrets(txt)


def test_masquer_secrets_couvre_email_et_cle(monkeypatch):
    monkeypatch.setenv("GARMIN_EMAIL", "alex@exemple.fr")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clef-sans-prefixe-standard")
    txt = "login alex@exemple.fr key=clef-sans-prefixe-standard"
    nettoye = masquer_secrets(txt)
    assert "alex@exemple.fr" not in nettoye
    assert "clef-sans-prefixe-standard" not in nettoye


def test_erreur_cli_masque_les_secrets(monkeypatch, capsys):
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2secret")
    cli.erreur("échec : password=hunter2secret")
    sortie = capsys.readouterr().err
    assert "hunter2secret" not in sortie


# ---- coach cron : jamais de clé en clair ----

def test_cron_ne_conseille_pas_la_cle_en_clair(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNNINGWHALE_HOME", str(tmp_path / "home"))
    assert cli.main(["--config", str(tmp_path / "absent.yml"), "cron"]) == 0
    sortie = capsys.readouterr().out
    # La variable ne se résout pas dans une crontab : la ligne ne doit plus l'interpoler.
    assert "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" not in sortie
    # L'approche conseillée est le fichier d'environnement restreint, sourcé par cron.
    assert "chmod 600" in sortie
    assert f". {tmp_path / 'home' / 'env'} &&" in sortie


# ---- élagage du JSON brut ----

def test_raw_est_epure_des_champs_sensibles(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    activite = activity_from_garmin(
        {
            "activityId": 42,
            "activityName": "Footing du matin",
            "startTimeLocal": "2026-08-10 07:30:00",
            "activityType": {"typeKey": "running"},
            "distance": 8000.0,
            "duration": 2400.0,
            "averageHR": 150,
            # Position de départ = adresse du domicile pour une sortie régulière
            "startLatitude": 48.85837,
            "startLongitude": 2.294481,
            "endLatitude": 48.8585,
            "endLongitude": 2.2946,
            "ownerId": 987654,
            "ownerFullName": "Alex Exemple",
            "ownerDisplayName": "alexe",
            "deviceId": 3999999999,
            "summaryDTO": {"startLatitude": 48.85837, "maxHR": 172},
        }
    )
    db.upsert_activity(activite)
    relue = db.get_activity("42")

    for champ in (
        "startLatitude", "startLongitude", "endLatitude", "endLongitude",
        "ownerId", "ownerFullName", "ownerDisplayName",
    ):
        assert champ not in relue.raw
    # L'élagage est récursif
    assert "startLatitude" not in relue.raw["summaryDTO"]
    # Le reste du brut est conservé pour corriger l'extraction a posteriori
    assert relue.raw["averageHR"] == 150
    assert relue.raw["deviceId"] == 3999999999
    assert relue.raw["summaryDTO"]["maxHR"] == 172


# ---- traversée de chemin ----

def test_slug_neutralise_les_noms_hostiles():
    hostiles = [
        "../../.ssh/authorized_keys",
        "..\\..\\evil",
        "／..／etc／passwd",  # solidus pleine chasse, normalisé NFKD en « / »
        "....",
    ]
    for nom in hostiles:
        propre = report.slug(nom)
        assert "/" not in propre and "\\" not in propre and ".." not in propre
        fichier = report.nom_fichier("debrief", date(2026, 8, 16), nom)
        assert "/" not in fichier and "\\" not in fichier and ".." not in fichier


def test_ecrire_refuse_un_nom_hors_dossier(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        report.ecrire(cfg, "../evasion.md", "contenu")
    assert not (tmp_path / "home" / "evasion.md").exists()
    # Un nom légitime passe toujours
    chemin = report.ecrire(cfg, "2026-08-16-bilan.md", "ok")
    assert chemin.parent == cfg.reports_dir


# ---- bornes du serveur MCP ----

mcp = pytest.importorskip("mcp")
from runningwhale import mcp_server  # noqa: E402


@pytest.fixture
def contexte_mcp(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    monkeypatch.setattr(mcp_server, "_config", cfg)
    monkeypatch.setattr(mcp_server, "_db", db)
    return cfg, db


def _seance(quand: date, **extra) -> dict:
    base = {
        "date": quand.isoformat(),
        "titre": "Footing",
        "type": "endurance",
        "distance_km": 10.0,
        "duree_min": 55.0,
        "objectif": "",
        "consignes": "",
        "etapes": [],
    }
    base.update(extra)
    return base


def _plan(seances) -> dict:
    return {
        "titre": "Semaine",
        "focus": "Endurance",
        "resume": "r",
        "points_de_vigilance": [],
        "volume_total_km": None,
        "seances": seances,
    }


def test_enregistrer_plan_refuse_10000_seances(contexte_mcp):
    seances = [_seance(date.today() + timedelta(days=i % 300)) for i in range(10_000)]
    with pytest.raises(ValueError, match="Trop de séances"):
        mcp_server.enregistrer_plan(_plan(seances))


def test_enregistrer_plan_refuse_un_plan_de_plusieurs_mo(contexte_mcp):
    plan = _plan([_seance(date.today())])
    plan["titre"] = "X" * 2_000_000
    with pytest.raises(ValueError, match="démesuré"):
        mcp_server.enregistrer_plan(plan)


def test_enregistrer_plan_refuse_une_date_illisible(contexte_mcp):
    with pytest.raises(ValueError, match="illisible"):
        mcp_server.enregistrer_plan(_plan([_seance(date.today(), date="n'importe quoi")]))


def test_enregistrer_plan_refuse_une_date_invraisemblable(contexte_mcp):
    with pytest.raises(ValueError, match="invraisemblable"):
        mcp_server.enregistrer_plan(_plan([_seance(date.today(), date="9999-12-31")]))


def test_enregistrer_plan_accepte_un_plan_raisonnable(contexte_mcp):
    cfg, db = contexte_mcp
    res = mcp_server.enregistrer_plan(
        _plan([_seance(date.today()), _seance(date.today() + timedelta(days=2))])
    )
    assert res["enregistre"] is True
    assert res["seances"] == 2


def test_recuperation_borne_une_fenetre_absurde(contexte_mcp):
    # jours=10^12 provoquait un OverflowError dans timedelta
    res = mcp_server.recuperation(jours=10**12)
    assert res["jours"] == []


def test_activites_recentes_borne_une_fenetre_absurde(contexte_mcp):
    assert mcp_server.activites_recentes(jours=10**12) == []
