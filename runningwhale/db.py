"""Stockage local SQLite : activités, tours, rapports générés, état de synchro."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from .composition import Composition
from .composition import from_row as composition_from_row
from .models import Activity, Lap
from .wellness import Wellness
from .wellness import from_row as wellness_from_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id              TEXT PRIMARY KEY,
    debut                    TEXT NOT NULL,
    type                     TEXT NOT NULL,
    nom                      TEXT,
    distance_m               REAL,
    duree_s                  REAL,
    duree_mouvement_s        REAL,
    denivele_pos_m           REAL,
    denivele_neg_m           REAL,
    fc_moy                   INTEGER,
    fc_max                   INTEGER,
    cadence_moy              INTEGER,
    puissance_moy            INTEGER,
    calories                 INTEGER,
    vo2max                   REAL,
    training_effect_aerobie  REAL,
    training_effect_anaerobie REAL,
    temperature_c            REAL,
    raw                      TEXT,
    ajoute_le                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_debut ON activities(debut);

CREATE TABLE IF NOT EXISTS laps (
    activity_id     TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    distance_m      REAL,
    duree_s         REAL,
    fc_moy          INTEGER,
    allure_s_km     REAL,
    denivele_pos_m  REAL,
    PRIMARY KEY (activity_id, idx)
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,          -- debrief | plan | question
    activity_id  TEXT,
    cree_le      TEXT NOT NULL,
    chemin       TEXT,
    contenu      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_kind ON reports(kind, cree_le);

CREATE TABLE IF NOT EXISTS wellness (
    jour                 TEXT PRIMARY KEY,
    sommeil_s            REAL,
    sommeil_profond_s    REAL,
    sommeil_paradoxal_s  REAL,
    score_sommeil        INTEGER,
    vfc_ms               REAL,
    vfc_statut           TEXT,
    vfc_baseline_bas     REAL,
    vfc_baseline_haut    REAL,
    fc_repos             INTEGER,
    body_battery_max     INTEGER,
    body_battery_min     INTEGER,
    stress_moyen         INTEGER,
    readiness_score      INTEGER,
    readiness_niveau     TEXT,
    temps_recup_h        REAL,
    raw                  TEXT,
    ajoute_le            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS composition (
    jour                     TEXT PRIMARY KEY,
    poids_kg                 REAL NOT NULL,
    imc                      REAL,
    graisse_pct              REAL,
    muscle_squelettique_pct  REAL,
    masse_hors_graisse_kg    REAL,
    gras_sous_cutane_pct     REAL,
    graisse_viscerale        REAL,
    eau_pct                  REAL,
    masse_musculaire_kg      REAL,
    masse_osseuse_kg         REAL,
    proteines_pct            REAL,
    metabolisme_base_kcal    INTEGER,
    age_metabolique          INTEGER,
    source                   TEXT,
    ajoute_le                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    cle    TEXT PRIMARY KEY,
    valeur TEXT
);
"""


# Champs écartés du JSON brut avant stockage. Les coordonnées de départ et
# d'arrivée d'une sortie régulière désignent le domicile ; les champs d'identité
# du compte n'apportent rien à l'analyse. Aucun n'est utilisé par l'extraction
# (`models.activity_from_garmin`), donc les élaguer ne compromet pas la
# possibilité de corriger l'extraction a posteriori — raison d'être du `raw`.
CHAMPS_RAW_SENSIBLES = frozenset(
    {
        "startLatitude",
        "startLongitude",
        "endLatitude",
        "endLongitude",
        "ownerId",
        "ownerFullName",
        "ownerDisplayName",
        "ownerProfileImageUrlSmall",
        "ownerProfileImageUrlMedium",
        "ownerProfileImageUrlLarge",
    }
)


def _epurer_raw(valeur: Any) -> Any:
    """Retire récursivement les champs sensibles d'une charge utile Garmin."""
    if isinstance(valeur, dict):
        return {
            k: _epurer_raw(v)
            for k, v in valeur.items()
            if k not in CHAMPS_RAW_SENSIBLES
        }
    if isinstance(valeur, list):
        return [_epurer_raw(v) for v in valeur]
    return valeur


def _migrer(conn: sqlite3.Connection) -> None:
    """Ajoute aux tables existantes les colonnes apparues depuis leur création.

    `CREATE TABLE IF NOT EXISTS` n'ajoute jamais de colonne à une table déjà en
    place : une base créée par une version antérieure ferait échouer chaque
    INSERT nommant une colonne récente. On compare chaque table au schéma de
    référence (instancié en mémoire) et on complète par `ALTER TABLE ADD COLUMN`.

    Seul l'ajout de colonnes est couvert — c'est la seule évolution que ce
    schéma ait connue. Une contrainte NOT NULL sans valeur par défaut n'est pas
    reportable sur une table peuplée : la colonne est alors ajoutée nullable,
    les lignes historiques n'ayant de toute façon pas la donnée.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(SCHEMA)
        for (table,) in reference.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            existantes = {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existantes:  # la table vient d'être créée complète
                continue
            for _, nom, type_, _, defaut, _ in reference.execute(
                f"PRAGMA table_info({table})"
            ).fetchall():
                if nom in existantes:
                    continue
                decl = f"{nom} {type_}" if type_ else nom
                if defaut is not None:
                    decl += f" DEFAULT {defaut}"
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
                except sqlite3.OperationalError as exc:
                    # Deux processus peuvent migrer en même temps (cron + MCP) :
                    # le second perd la course, la colonne est déjà là.
                    if "duplicate column" not in str(exc).lower():
                        raise
    finally:
        reference.close()


class Database:
    """Accès SQLite. Toutes les écritures d'activités sont idempotentes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            _migrer(conn)
        # La base contient l'historique de santé complet : lisible par
        # l'utilisateur seul, y compris si elle a été créée avec le umask
        # par défaut par une version antérieure.
        try:
            self.path.chmod(0o600)
        except OSError:  # système de fichiers sans permissions POSIX
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- activités ----

    def upsert_activity(self, activity: Activity) -> bool:
        """Insère ou met à jour. Renvoie True si l'activité était nouvelle."""
        row = activity.to_row()
        row["raw"] = json.dumps(_epurer_raw(activity.raw), ensure_ascii=False)
        row["ajoute_le"] = datetime.now().isoformat(timespec="seconds")

        with self._connect() as conn:
            existait = conn.execute(
                "SELECT 1 FROM activities WHERE activity_id = ?",
                (activity.activity_id,),
            ).fetchone()

            colonnes = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT OR REPLACE INTO activities ({colonnes}) VALUES ({placeholders})",
                row,
            )
            if activity.laps:
                conn.execute(
                    "DELETE FROM laps WHERE activity_id = ?", (activity.activity_id,)
                )
                conn.executemany(
                    "INSERT INTO laps (activity_id, idx, distance_m, duree_s, fc_moy,"
                    " allure_s_km, denivele_pos_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            activity.activity_id,
                            lap.index,
                            lap.distance_m,
                            lap.duree_s,
                            lap.fc_moy,
                            lap.allure_s_km,
                            lap.denivele_pos_m,
                        )
                        for lap in activity.laps
                    ],
                )
        return existait is None

    def get_activity(self, activity_id: str) -> Activity | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            if row is None:
                return None
            laps = conn.execute(
                "SELECT * FROM laps WHERE activity_id = ? ORDER BY idx", (activity_id,)
            ).fetchall()
        return _activity_from_row(row, laps)

    def activities(
        self, depuis: datetime | None = None, seulement_course: bool = False
    ) -> list[Activity]:
        clauses, params = [], []
        if depuis is not None:
            clauses.append("debut >= ?")
            params.append(depuis.isoformat())
        if seulement_course:
            clauses.append("(type LIKE '%running%' OR type LIKE '%trail%')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM activities {where} ORDER BY debut DESC", params
            ).fetchall()
        return [_activity_from_row(r, []) for r in rows]

    def last_activity(self, seulement_course: bool = True) -> Activity | None:
        acts = self.activities(seulement_course=seulement_course)
        if not acts:
            return None
        return self.get_activity(acts[0].activity_id)

    def latest_start(self) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(debut) AS m FROM activities").fetchone()
        if not row or not row["m"]:
            return None
        return datetime.fromisoformat(row["m"])

    def count_activities(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM activities").fetchone()["c"]

    # ---- récupération ----

    def upsert_wellness(self, jour_data: Wellness) -> bool:
        """Insère ou met à jour une journée. Renvoie True si elle était nouvelle."""
        row = jour_data.to_row()
        row["raw"] = json.dumps(jour_data.raw, ensure_ascii=False)
        row["ajoute_le"] = datetime.now().isoformat(timespec="seconds")

        with self._connect() as conn:
            existait = conn.execute(
                "SELECT 1 FROM wellness WHERE jour = ?", (row["jour"],)
            ).fetchone()
            colonnes = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT OR REPLACE INTO wellness ({colonnes}) VALUES ({placeholders})",
                row,
            )
        return existait is None

    def wellness(self, depuis: date | None = None) -> list[Wellness]:
        """Journées de récupération, de la plus récente à la plus ancienne."""
        where, params = "", []
        if depuis is not None:
            where = "WHERE jour >= ?"
            params.append(depuis.isoformat())
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM wellness {where} ORDER BY jour DESC", params
            ).fetchall()
        return [wellness_from_row(r) for r in rows]

    def wellness_du_jour(self, jour: date) -> Wellness | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wellness WHERE jour = ?", (jour.isoformat(),)
            ).fetchone()
        return wellness_from_row(row) if row else None

    def dernier_jour_wellness(self) -> date | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(jour) AS m FROM wellness").fetchone()
        return date.fromisoformat(row["m"]) if row and row["m"] else None

    # ---- rapports ----

    def save_report(
        self, kind: str, contenu: str, activity_id: str | None = None,
        chemin: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reports (kind, activity_id, cree_le, chemin, contenu)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    kind,
                    activity_id,
                    datetime.now().isoformat(timespec="seconds"),
                    chemin,
                    contenu,
                ),
            )
            return int(cur.lastrowid)

    def has_report(self, kind: str, activity_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM reports WHERE kind = ? AND activity_id = ?",
                (kind, activity_id),
            ).fetchone()
        return row is not None

    def recent_reports(self, kind: str | None = None, limite: int = 5) -> list[dict[str, Any]]:
        where = "WHERE kind = ?" if kind else ""
        params = [kind] if kind else []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM reports {where} ORDER BY cree_le DESC LIMIT ?",
                [*params, limite],
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- méta ----

    # ---- composition corporelle ----

    def upsert_composition(self, mesure: Composition) -> bool:
        """Enregistre une pesée. Renvoie True si la journée était nouvelle.

        Une seule pesée par jour : se peser deux fois le même matin donne deux
        chiffres différents sans qu'aucun soit plus vrai, et la dernière mesure
        remplace simplement la précédente.
        """
        row = mesure.to_row()
        row["ajoute_le"] = datetime.now().isoformat(timespec="seconds")

        with self._connect() as conn:
            existait = conn.execute(
                "SELECT 1 FROM composition WHERE jour = ?", (row["jour"],)
            ).fetchone()
            colonnes = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT OR REPLACE INTO composition ({colonnes}) VALUES ({placeholders})",
                row,
            )
        return existait is None

    def compositions(self, depuis: date | None = None) -> list[Composition]:
        clauses, params = [], []
        if depuis is not None:
            clauses.append("jour >= ?")
            params.append(depuis.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM composition {where} ORDER BY jour ASC", params
            ).fetchall()
        return [composition_from_row(r) for r in rows]

    def derniere_composition(self) -> Composition | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composition ORDER BY jour DESC LIMIT 1"
            ).fetchone()
        return composition_from_row(row) if row else None

    def set_meta(self, cle: str, valeur: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (cle, valeur) VALUES (?, ?)", (cle, valeur)
            )

    def get_meta(self, cle: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT valeur FROM meta WHERE cle = ?", (cle,)).fetchone()
        return row["valeur"] if row else None


def _activity_from_row(row: sqlite3.Row, lap_rows: list[sqlite3.Row]) -> Activity:
    raw = {}
    if "raw" in row.keys() and row["raw"]:
        try:
            raw = json.loads(row["raw"])
        except json.JSONDecodeError:
            raw = {}
    return Activity(
        activity_id=row["activity_id"],
        debut=datetime.fromisoformat(row["debut"]),
        type=row["type"],
        nom=row["nom"] or "",
        distance_m=row["distance_m"] or 0.0,
        duree_s=row["duree_s"] or 0.0,
        duree_mouvement_s=row["duree_mouvement_s"],
        denivele_pos_m=row["denivele_pos_m"],
        denivele_neg_m=row["denivele_neg_m"],
        fc_moy=row["fc_moy"],
        fc_max=row["fc_max"],
        cadence_moy=row["cadence_moy"],
        puissance_moy=row["puissance_moy"],
        calories=row["calories"],
        vo2max=row["vo2max"],
        training_effect_aerobie=row["training_effect_aerobie"],
        training_effect_anaerobie=row["training_effect_anaerobie"],
        temperature_c=row["temperature_c"],
        laps=[
            Lap(
                index=lr["idx"],
                distance_m=lr["distance_m"] or 0.0,
                duree_s=lr["duree_s"] or 0.0,
                fc_moy=lr["fc_moy"],
                allure_s_km=lr["allure_s_km"],
                denivele_pos_m=lr["denivele_pos_m"],
            )
            for lr in lap_rows
        ],
        raw=raw,
    )
