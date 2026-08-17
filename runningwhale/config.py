"""Chargement de la configuration : profil athlète, objectifs, chemins de travail."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


def _home() -> Path:
    """Racine de travail : données, tokens, rapports."""
    return Path(os.environ.get("RUNNINGWHALE_HOME", Path.home() / ".runningwhale"))


@dataclass
class Race:
    """Une course objectif."""

    nom: str
    date: date
    distance_km: float
    objectif_temps_s: int | None = None
    denivele_m: int | None = None
    priorite: str = "A"

    @property
    def semaines_restantes(self) -> float:
        return (self.date - date.today()).days / 7.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Race":
        raw_date = d["date"]
        if isinstance(raw_date, str):
            raw_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        return cls(
            nom=d["nom"],
            date=raw_date,
            distance_km=float(d["distance_km"]),
            objectif_temps_s=_parse_duration(d.get("objectif_temps")),
            denivele_m=d.get("denivele_m"),
            priorite=d.get("priorite", "A"),
        )


@dataclass
class Athlete:
    """Profil de l'athlète. Tout est optionnel sauf le prénom."""

    prenom: str = "Athlète"
    # `naissance` est préférable à `age` : un âge écrit en dur se périme en
    # silence, et la FC max théorique comme les projections vieillissent avec
    # lui. Quand les deux sont donnés, la date de naissance l'emporte.
    naissance: date | None = None
    age_declare: int | None = None
    taille_cm: float | None = None
    sexe: str | None = None  # "H" | "F" — utilisé par la formule TRIMP de Banister
    poids_kg: float | None = None
    fc_max: int | None = None
    fc_repos: int | None = None
    fc_seuil: int | None = None  # LTHR, FC moyenne sur un effort seuil de 30-60 min
    allure_seuil_s_km: int | None = None  # allure seuil en secondes par km
    vma_kmh: float | None = None
    seances_par_semaine: int | None = None
    volume_hebdo_cible_km: float | None = None
    historique: str = ""  # texte libre : blessures, antécédents, contraintes
    contraintes: str = ""  # texte libre : dispos, terrain, matériel
    objectifs: list[Race] = field(default_factory=list)

    @property
    def age(self) -> int | None:
        """Âge d'aujourd'hui, calculé depuis la naissance si elle est connue."""
        if self.naissance is not None:
            aujourdhui = date.today()
            revolu = (aujourdhui.month, aujourdhui.day) >= (
                self.naissance.month,
                self.naissance.day,
            )
            return aujourdhui.year - self.naissance.year - (0 if revolu else 1)
        return self.age_declare

    @property
    def imc(self) -> float | None:
        """Indice de masse corporelle, si taille et poids sont connus.

        Chez un coureur costaud, l'IMC surestime l'adiposité : il ne distingue
        pas le muscle de la graisse. À ne lire qu'avec le pourcentage de masse
        grasse à côté.
        """
        if not self.taille_cm or not self.poids_kg or self.taille_cm <= 0:
            return None
        metres = self.taille_cm / 100.0
        return self.poids_kg / (metres * metres)

    @property
    def fc_reserve(self) -> int | None:
        if self.fc_max and self.fc_repos:
            return self.fc_max - self.fc_repos
        return None

    @property
    def prochain_objectif(self) -> Race | None:
        futurs = sorted(
            (r for r in self.objectifs if r.date >= date.today()),
            key=lambda r: r.date,
        )
        return futurs[0] if futurs else None


@dataclass
class Config:
    athlete: Athlete
    home: Path
    db_path: Path
    reports_dir: Path
    token_dir: Path
    config_path: Path
    modele: str = "claude-opus-5"
    effort: str = "high"
    langue: str = "français"

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")


def _parse_duration(value: Any) -> int | None:
    """Accepte 3600, "1:30:00", "45:00" ou "3h30" et renvoie des secondes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    txt = str(value).strip().lower().replace("h", ":").replace("min", ":")
    txt = txt.rstrip(":") if txt.endswith(":") else txt
    parts = [p for p in txt.split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, nums[0], 0
    else:
        return None
    return h * 3600 + m * 60 + s


def _parse_pace(value: Any) -> int | None:
    """Accepte "4:30" ou 270 et renvoie des secondes par km."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    parts = str(value).strip().split(":")
    if len(parts) == 2:
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
    return _parse_duration(value)


def _parse_date(value: Any) -> date | None:
    """Accepte une date YAML, `1990-05-01`, ou le mois seul `1990-05`.

    Quand seul le mois est connu, on prend le premier du mois : l'âge qui en
    découle est juste à quelques semaines près, ce qui suffit largement — bien
    mieux qu'un âge en dur qui se périme d'un an chaque année.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    txt = str(value).strip()
    for fmt, complet in (("%Y-%m-%d", True), ("%Y/%m/%d", True), ("%Y-%m", False)):
        try:
            lu = datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
        return lu if complet else lu.replace(day=1)
    return None


def _taille_en_cm(value: Any) -> float | None:
    """Accepte `178`, `1.78` ou `"1m78"` et renvoie des centimètres.

    Écrire sa taille en mètres est le réflexe le plus courant ; la prendre pour
    des centimètres donnerait un IMC absurde plutôt qu'une erreur visible.
    """
    if value is None or value == "":
        return None
    txt = str(value).strip().lower().replace(",", ".")
    if "m" in txt and not txt.endswith("cm"):
        # « 1m78 » : mètres et centimètres de part et d'autre du « m »
        avant, _, apres = txt.partition("m")
        try:
            metres = float(avant or 0)
            reste = float(apres) if apres.strip() else 0.0
        except ValueError:
            return None
        return metres * 100 + reste
    txt = txt.removesuffix("cm").strip()
    try:
        nombre = float(txt)
    except ValueError:
        return None
    # Sous 3, c'est forcément des mètres : personne ne mesure 2 cm.
    return nombre * 100 if nombre < 3 else nombre


def default_config_path() -> Path:
    """Emplacement du fichier de configuration.

    Par ordre de priorité : la variable `RUNNINGWHALE_CONFIG`, puis
    `./config/athlete.yml`, puis `~/.runningwhale/athlete.yml`.

    La variable d'environnement est indispensable pour le serveur MCP : il est
    lancé par un client (Claude Desktop, Claude Code) depuis un répertoire de
    travail arbitraire, où le chemin relatif ne veut rien dire.
    """
    explicite = os.environ.get("RUNNINGWHALE_CONFIG")
    if explicite:
        return Path(explicite).expanduser()

    local = Path.cwd() / "config" / "athlete.yml"
    if local.exists():
        return local
    return _home() / "athlete.yml"


def load_config(config_path: Path | None = None) -> Config:
    home = _home()
    path = Path(config_path) if config_path else default_config_path()

    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    a = raw.get("athlete", {}) or {}
    athlete = Athlete(
        prenom=a.get("prenom", "Athlète"),
        naissance=_parse_date(a.get("naissance")),
        age_declare=a.get("age"),
        taille_cm=_taille_en_cm(a.get("taille_cm") or a.get("taille")),
        sexe=a.get("sexe"),
        poids_kg=a.get("poids_kg"),
        fc_max=a.get("fc_max"),
        fc_repos=a.get("fc_repos"),
        fc_seuil=a.get("fc_seuil"),
        allure_seuil_s_km=_parse_pace(a.get("allure_seuil")),
        vma_kmh=a.get("vma_kmh"),
        seances_par_semaine=a.get("seances_par_semaine"),
        volume_hebdo_cible_km=a.get("volume_hebdo_cible_km"),
        historique=a.get("historique", "") or "",
        contraintes=a.get("contraintes", "") or "",
        objectifs=[Race.from_dict(r) for r in (raw.get("objectifs") or [])],
    )

    coach_cfg = raw.get("coach", {}) or {}
    return Config(
        athlete=athlete,
        home=home,
        db_path=home / "runningwhale.db",
        reports_dir=Path(coach_cfg.get("reports_dir") or (home / "reports")),
        token_dir=home / "garmin_tokens",
        config_path=path,
        modele=coach_cfg.get("modele", "claude-opus-5"),
        effort=coach_cfg.get("effort", "high"),
        langue=coach_cfg.get("langue", "français"),
    )


def _dossier_prive(path: Path) -> None:
    """Crée le dossier s'il manque et le restreint à l'utilisateur (0700).

    Le contenu — base de santé complète, jetons Garmin, rapports — n'a rien à
    faire en lecture pour les autres comptes de la machine. On resserre aussi
    un dossier existant, créé par une version antérieure avec le umask par défaut.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:  # système de fichiers sans permissions POSIX
        pass


def ensure_dirs(cfg: Config) -> None:
    _dossier_prive(cfg.home)
    _dossier_prive(cfg.reports_dir)
    _dossier_prive(cfg.token_dir)
