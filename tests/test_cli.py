"""Tests du CLI sur les modes de défaillance : plan corrompu, watch chevauchant."""

from __future__ import annotations

import fcntl
from datetime import datetime, timedelta

import pytest

from runningwhale import cli
from runningwhale.db import Database
from runningwhale.models import Activity

PLANS_CORROMPUS = [
    pytest.param('{"titre": "x", "sea', id="json-tronque"),
    pytest.param("null", id="null"),
    pytest.param("[1, 2, 3]", id="liste"),
    pytest.param('"bonjour"', id="chaine"),
    pytest.param("42", id="nombre"),
    pytest.param('{"seances": "abc"}', id="seances-non-liste"),
    pytest.param('{"seances": [42]}', id="seance-non-dict"),
    pytest.param('{"seances": [{"date": "2026-08-17", "etapes": "abc"}]}',
                 id="etapes-chaine"),
    pytest.param('{"seances": [{"date": "2026-08-17", "etapes": [7]}]}',
                 id="etape-non-dict"),
    pytest.param('{"seances": [], "genere_le": 123}', id="genere_le-nombre"),
    pytest.param('{"seances": [], "genere_le": "hier"}', id="genere_le-illisible"),
]


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite")


@pytest.mark.parametrize("brut", PLANS_CORROMPUS)
def test_plan_corrompu_est_traite_comme_absent(db, brut):
    """`Plan.from_json` lève ValueError, mais aussi TypeError et AttributeError
    sur une entrée abîmée : aucun de ces cas ne doit remonter jusqu'à
    `coach status`."""
    db.set_meta(cli.CLE_PLAN_COURANT, brut)
    assert cli._plan_courant(db) is None
    assert cli._adherence(db) is None


def test_coach_status_survit_a_un_plan_corrompu(tmp_path, monkeypatch, capsys):
    """Bout en bout : la commande `status` s'affiche malgré un plan illisible."""
    monkeypatch.setenv("RUNNINGWHALE_HOME", str(tmp_path))
    config = tmp_path / "athlete.yml"
    config.write_text("athlete:\n  prenom: Test\n", encoding="utf-8")

    db = Database(tmp_path / "runningwhale.db")
    db.upsert_activity(
        Activity(
            activity_id="a1",
            debut=datetime.now() - timedelta(days=1),
            type="running",
            distance_m=10000,
            duree_s=3000,
            duree_mouvement_s=3000,
            fc_moy=150,
        )
    )
    db.set_meta(cli.CLE_PLAN_COURANT, '{"seances": [42]}')

    code = cli.main(["--config", str(config), "status"])
    sortie = capsys.readouterr().out
    assert code == 0
    assert "Bilan d'entraînement" in sortie


# ---- verrou de `coach watch` ----

def test_verrou_exclusif_refuse_un_second_detenteur(tmp_path):
    verrou = tmp_path / "watch.lock"
    with cli._verrou_exclusif(verrou) as premier:
        assert premier is True
        # Un descripteur indépendant (comme en aurait un second processus)
        # doit se voir refuser le verrou tant que le premier le détient.
        with open(verrou, "w") as fh:
            with pytest.raises(OSError):
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Une fois relâché, il redevient disponible.
    with cli._verrou_exclusif(verrou) as second:
        assert second is True


def test_watch_chevauchant_ne_declenche_pas_de_double_debrief(tmp_path, monkeypatch, capsys):
    """Deux `coach watch` simultanés verraient la même course comme nouvelle et
    la débrieferaient deux fois. Le second doit s'effacer sans toucher à rien."""
    monkeypatch.setenv("RUNNINGWHALE_HOME", str(tmp_path))
    config = tmp_path / "athlete.yml"
    config.write_text("athlete:\n  prenom: Test\n", encoding="utf-8")

    def connexion_interdite(*args, **kwargs):
        raise AssertionError("le second watch ne doit pas synchroniser")

    monkeypatch.setattr(cli, "connect", connexion_interdite)

    # Le « premier » watch détient le verrou (descripteur indépendant, comme un
    # autre processus cron encore en train de débriefer).
    verrou = tmp_path / "watch.lock"
    with open(verrou, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = cli.main(["--config", str(config), "watch"])

    assert code == 0
    assert "déjà en cours" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Espacement des synchros : `coach sync --espacer H`
# ---------------------------------------------------------------------------

@pytest.fixture
def maison(tmp_path, monkeypatch):
    """Un RUNNINGWHALE_HOME jetable avec un profil minimal."""
    monkeypatch.setenv("RUNNINGWHALE_HOME", str(tmp_path))
    (tmp_path / "athlete.yml").write_text(
        "athlete:\n  prenom: Test\n", encoding="utf-8"
    )
    return tmp_path


def _poser_derniere_synchro(home, quand: datetime) -> None:
    db = Database(home / "runningwhale.db")
    db.set_meta("derniere_synchro", quand.isoformat(timespec="seconds"))


def test_espacer_saute_une_synchro_recente(maison, monkeypatch, capsys):
    """Synchro d'il y a 1 h, espacement 3 h : aucun contact avec Garmin."""
    _poser_derniere_synchro(maison, datetime.now() - timedelta(hours=1))

    def connexion_interdite(*a, **k):  # pragma: no cover - ne doit pas arriver
        raise AssertionError("connect() appelé alors que la synchro devait être sautée")

    monkeypatch.setattr(cli, "connect", connexion_interdite)
    assert cli.main(["sync", "--espacer", "3"]) == 0
    sorties = capsys.readouterr()
    assert "rien à refaire" in (sorties.err + sorties.out).lower()


def test_espacer_laisse_passer_une_synchro_ancienne(maison, monkeypatch):
    """Synchro d'il y a 5 h, espacement 3 h : la connexion est bien tentée."""
    _poser_derniere_synchro(maison, datetime.now() - timedelta(hours=5))
    appels = []

    def connexion_factice(*a, **k):
        appels.append(True)
        raise cli.GarminError("stop ici, le test ne va pas plus loin")

    monkeypatch.setattr(cli, "connect", connexion_factice)
    assert cli.main(["sync", "--espacer", "3"]) == 1
    assert appels, "connect() aurait dû être appelé"


def test_espacer_synchronise_sur_base_vierge(maison, monkeypatch):
    """Aucune synchro connue : dans le doute, on synchronise."""
    appels = []

    def connexion_factice(*a, **k):
        appels.append(True)
        raise cli.GarminError("stop ici")

    monkeypatch.setattr(cli, "connect", connexion_factice)
    assert cli.main(["sync", "--espacer", "3"]) == 1
    assert appels


def test_espacer_ignore_une_date_illisible(maison, monkeypatch):
    """Une méta corrompue compte comme inconnue : on synchronise."""
    db = Database(maison / "runningwhale.db")
    db.set_meta("derniere_synchro", "pas-une-date")
    appels = []

    def connexion_factice(*a, **k):
        appels.append(True)
        raise cli.GarminError("stop ici")

    monkeypatch.setattr(cli, "connect", connexion_factice)
    assert cli.main(["sync", "--espacer", "3"]) == 1
    assert appels


def test_espacer_resynchronise_si_horloge_recule(maison, monkeypatch):
    """Dernière synchro « dans le futur » (horloge changée) : on synchronise."""
    _poser_derniere_synchro(maison, datetime.now() + timedelta(hours=2))
    appels = []

    def connexion_factice(*a, **k):
        appels.append(True)
        raise cli.GarminError("stop ici")

    monkeypatch.setattr(cli, "connect", connexion_factice)
    assert cli.main(["sync", "--espacer", "3"]) == 1
    assert appels
