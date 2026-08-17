"""Tests d'intégrité de la base : atomicité, migration, accès concurrent.

L'outil tourne sans surveillance la nuit : ces tests vérifient que la base
survit aux interruptions, aux vieilles versions du schéma et aux processus
simultanés (cron `coach watch` + serveur MCP + commande manuelle).
"""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from runningwhale.db import Database
from runningwhale.models import Activity, Lap
from runningwhale.wellness import Wellness


def activite(aid: str, jours_avant: int = 0, laps: list[Lap] | None = None) -> Activity:
    return Activity(
        activity_id=aid,
        debut=datetime(2026, 8, 10, 9, 0) - timedelta(days=jours_avant),
        type="running",
        nom=f"Sortie {aid}",
        distance_m=10000.0,
        duree_s=3000.0,
        laps=laps or [],
        raw={"activityId": aid},
    )


# ---- atomicité ----

def test_upsert_activity_est_une_seule_transaction(tmp_path):
    """Un échec entre le DELETE des tours et leur INSERT ne doit rien laisser
    à moitié fait : ni tours perdus, ni activité mise à jour sans ses tours."""
    db = Database(tmp_path / "t.sqlite")
    db.upsert_activity(
        activite("a1", laps=[Lap(index=0, distance_m=1000.0, duree_s=300.0)])
    )

    # Deuxième upsert dont un tour est non liable : l'INSERT des tours échoue
    # après que le DELETE et le REPLACE de l'activité ont été exécutés.
    corrompue = activite("a1", laps=[Lap(index=0, distance_m=object(), duree_s=1.0)])
    corrompue.distance_m = 99999.0
    with pytest.raises(sqlite3.Error):
        db.upsert_activity(corrompue)

    apres = db.get_activity("a1")
    assert len(apres.laps) == 1, "les tours d'origine doivent survivre au rollback"
    assert apres.distance_m == 10000.0, "la mise à jour partielle doit être annulée"


def test_json_brut_corrompu_ne_casse_pas_la_lecture(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    db.upsert_activity(activite("a1"))

    # Un JSON tronqué (écriture interrompue, corruption disque) en base
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "UPDATE activities SET raw = ? WHERE activity_id = ?",
            ('{"activityId": "a1", "tronq', "a1"),
        )

    lue = db.get_activity("a1")
    assert lue is not None
    assert lue.raw == {}


# ---- migration de schéma ----

ANCIEN_SCHEMA = """
CREATE TABLE activities (
    activity_id              TEXT PRIMARY KEY,
    debut                    TEXT NOT NULL,
    type                     TEXT NOT NULL,
    nom                      TEXT,
    distance_m               REAL,
    duree_s                  REAL,
    fc_moy                   INTEGER,
    raw                      TEXT,
    ajoute_le                TEXT NOT NULL
);
CREATE TABLE wellness (
    jour                 TEXT PRIMARY KEY,
    sommeil_s            REAL,
    vfc_ms               REAL,
    raw                  TEXT,
    ajoute_le            TEXT NOT NULL
);
CREATE TABLE meta (
    cle    TEXT PRIMARY KEY,
    valeur TEXT
);
"""


def test_migration_complete_les_colonnes_d_une_vieille_base(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` n'ajoute pas de colonne : une base d'une
    version antérieure doit être complétée à l'ouverture, sans perdre ses données."""
    chemin = tmp_path / "vieille.sqlite"
    with sqlite3.connect(chemin) as conn:
        conn.executescript(ANCIEN_SCHEMA)
        conn.execute(
            "INSERT INTO activities (activity_id, debut, type, nom, distance_m,"
            " duree_s, fc_moy, raw, ajoute_le) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("historique", "2025-06-01T09:00:00", "running", "Vieille sortie",
             12000.0, 3600.0, 150, "{}", "2025-06-01T12:00:00"),
        )

    db = Database(chemin)

    # Les anciennes données sont intactes, les nouvelles colonnes lisibles
    ancienne = db.get_activity("historique")
    assert ancienne.distance_m == 12000.0
    assert ancienne.vo2max is None  # colonne ajoutée par la migration

    # Les écritures utilisant les colonnes récentes fonctionnent
    db.upsert_activity(activite("nouvelle"))
    db.upsert_wellness(Wellness(jour=date(2026, 8, 15), sommeil_s=27000.0,
                                readiness_score=70))
    assert db.count_activities() == 2
    assert db.wellness_du_jour(date(2026, 8, 15)).readiness_score == 70


def test_migration_idempotente(tmp_path):
    chemin = tmp_path / "t.sqlite"
    Database(chemin)
    db = Database(chemin)  # deuxième ouverture : rien à migrer, rien ne casse
    db.upsert_activity(activite("a1"))
    assert db.count_activities() == 1


# ---- accès concurrent ----

def _ecrivain(chemin: str, prefixe: str, n: int, erreurs) -> None:
    try:
        db = Database(chemin)
        for i in range(n):
            db.upsert_activity(activite(f"{prefixe}-{i}", jours_avant=i % 30))
            db.set_meta(f"cle-{prefixe}", str(i))
    except Exception as exc:  # noqa: BLE001 — remonté au test
        erreurs.put(f"{prefixe}: {type(exc).__name__}: {exc}")


def _lecteur(chemin: str, n: int, erreurs) -> None:
    try:
        db = Database(chemin)
        for _ in range(n):
            db.activities()
            db.count_activities()
            db.latest_start()
    except Exception as exc:  # noqa: BLE001
        erreurs.put(f"lecteur: {type(exc).__name__}: {exc}")


def test_acces_concurrent_multi_processus(tmp_path):
    """Cron, serveur MCP et commande manuelle peuvent écrire en même temps.

    Le schéma « connexion ouverte/fermée par appel » plus le busy_timeout de
    5 s par défaut de sqlite3 doivent absorber la contention sans jamais lever
    `database is locked` ni perdre une écriture.
    """
    chemin = str(tmp_path / "concurrent.sqlite")
    erreurs = multiprocessing.Queue()
    ecrivains, par_processus = 4, 25

    procs = [
        multiprocessing.Process(
            target=_ecrivain, args=(chemin, f"p{i}", par_processus, erreurs)
        )
        for i in range(ecrivains)
    ]
    procs += [
        multiprocessing.Process(target=_lecteur, args=(chemin, 30, erreurs))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    rencontrees = []
    while not erreurs.empty():
        rencontrees.append(erreurs.get())
    assert not rencontrees, f"erreurs concurrentes : {rencontrees}"
    assert Database(chemin).count_activities() == ecrivains * par_processus
