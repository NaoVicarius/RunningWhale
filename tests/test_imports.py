"""Tests de l'import de fichiers exportés.

Aucun réseau : ces tests portent sur ce qui entre en base quand les données
arrivent à la main plutôt que par la synchronisation en ligne — les unités de
chaque format, et surtout le dédoublonnage, puisque compter deux fois la même
séance fausserait toute la charge d'entraînement.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime

import pytest

from runningwhale.db import Database
from runningwhale.imports import (
    ImportError_,
    ResultatImport,
    _duree_csv,
    _nombre_csv,
    collecter,
    dedoublonner,
    importer,
    lire_csv,
    lire_gpx,
    lire_json,
    lire_tcx,
)
from runningwhale.models import Activity, Lap


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "test.db")


# ---- export complet du compte (JSON) ----

EXPORT_COMPTE = json.dumps(
    [
        {
            "summarizedActivitiesExport": [
                {
                    "activityId": 987654321,
                    "name": "Sortie longue",
                    "activityType": "running",
                    # epoch ms, distance en centimètres, durées en millisecondes
                    "beginTimestamp": 1754809800000,
                    "distance": 1500000,  # 15 km
                    "duration": 4500000,  # 1 h 15
                    "movingDuration": 4470000,
                    "elevationGain": 12000,  # 120 m
                    "avgHr": 148,
                    "maxHr": 171,
                    "calories": 1010,
                    "vO2MaxValue": 52.0,
                    "aerobicTrainingEffect": 3.4,
                }
            ]
        }
    ]
)


def test_export_de_compte_convertit_centimetres_et_millisecondes():
    activites = lire_json(EXPORT_COMPTE)
    assert len(activites) == 1
    a = activites[0]
    assert a.activity_id == "987654321"
    assert a.distance_m == pytest.approx(15000.0)
    assert a.duree_s == pytest.approx(4500.0)
    assert a.duree_mouvement_s == pytest.approx(4470.0)
    assert a.denivele_pos_m == pytest.approx(120.0)
    assert a.fc_moy == 148
    assert a.type == "running"
    # Une allure de sortie longue, pas un chiffre absurde
    assert 240 < a.allure_s_km < 400


def test_reponse_de_l_api_connect_garde_metres_et_secondes():
    # Même fonction, autre schéma : l'API Connect date par startTimeLocal et
    # parle en mètres. Confondre les deux donnerait un 10 km à 1000 km.
    brut = json.dumps(
        [
            {
                "activityId": 42,
                "startTimeLocal": "2026-08-10 07:30:00",
                "activityType": {"typeKey": "running"},
                "distance": 10000.0,
                "duration": 3000.0,
                "averageHR": 150,
            }
        ]
    )
    a = lire_json(brut)[0]
    assert a.distance_m == pytest.approx(10000.0)
    assert a.duree_s == pytest.approx(3000.0)
    assert a.fc_moy == 150


def test_entree_sans_date_est_ecartee_sans_casser_le_fichier():
    brut = json.dumps(
        [{"summarizedActivitiesExport": [
            {"activityId": 1, "beginTimestamp": None},
            {"activityId": 2, "beginTimestamp": 1754809800000, "distance": 500000,
             "duration": 1800000, "activityType": "running"},
        ]}]
    )
    activites = lire_json(brut)
    assert [a.activity_id for a in activites] == ["2"]


def test_json_illisible_est_signale():
    with pytest.raises(ImportError_):
        lire_json("{ pas du json")


# ---- CSV de la liste d'activités ----

# Extrait fidèle d'un export réel (compte en français) : en-têtes traduits,
# nombres entre guillemets, `--` pour les mesures absentes, apostrophe de
# protection Excel, et virgule séparatrice de milliers sur les pas.
CSV_GARMIN = (
    "Type d'activité,Date,Favori,Titre,Distance,Calories,Durée,"
    "Fréquence cardiaque moyenne,Fréquence cardiaque maximale,TE aérobie,"
    "Cadence de course moyenne,Ascension totale,Descente totale,Pas,"
    "Consommation du Body Battery,Température minimale,Temps de déplacement\n"
    'Course à pied,2026-08-10 10:52:21,false,"Sortie du matin","3.95",'
    '"407","00:35:20","151","182","3.3","121","7","5","4,404","\'-12","31.0","00:35:08"\n'
    'Musculation,2026-06-20 10:04:15,false,"Musculation","0.00","540","00:57:59",'
    '"129","164","3.2","--","--","--","--","\'-17","--","00:50:21"\n'
    'Randonnée,2026-05-16 13:18:54,false,"Randonnée en forêt","9.70","943",'
    '"02:49:38","105","159","2.9","65","322","329","12,426","\'-21","--","01:57:11"\n'
)


def test_csv_lit_les_kilometres_et_les_durees():
    activites = lire_csv(CSV_GARMIN)
    assert len(activites) == 3
    course = activites[0]
    # Le CSV est le seul format Garmin à compter en kilomètres
    assert course.distance_m == pytest.approx(3950.0)
    assert course.duree_s == pytest.approx(2120.0)  # 00:35:20
    assert course.duree_mouvement_s == pytest.approx(2108.0)
    assert course.fc_moy == 151 and course.fc_max == 182
    assert course.denivele_pos_m == pytest.approx(7.0)
    assert course.training_effect_aerobie == pytest.approx(3.3)
    assert course.nom == "Sortie du matin"
    # L'allure se calcule sur le temps de déplacement, comme partout ailleurs
    # dans le coach : 2108 s / 3,95 km. Garmin affiche 8:56/km parce qu'il
    # divise par la durée totale — deux conventions, deux chiffres proches.
    assert course.allure_s_km == pytest.approx(2108.0 / 3.95, abs=1)


def test_csv_traduit_les_types_francais():
    types = [a.type for a in lire_csv(CSV_GARMIN)]
    assert types == ["running", "strength_training", "hiking"]


def test_csv_accepte_des_entetes_anglais():
    anglais = (
        "Activity Type,Date,Title,Distance,Time,Avg HR\n"
        'Running,2026-08-10 10:52:21,"Morning Run","3.95","00:35:20","151"\n'
    )
    a = lire_csv(anglais)[0]
    assert a.type == "running"
    assert a.distance_m == pytest.approx(3950.0)


def test_csv_sans_colonne_de_date_est_refuse():
    with pytest.raises(ImportError_):
        lire_csv("Type d'activité,Distance\nCourse à pied,3.95\n")


@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("4,404", 4404.0),      # virgule séparatrice de milliers
        ("12,426", 12426.0),
        ("3,95", 3.95),         # virgule décimale (compte en français)
        ("1.234,5", 1234.5),    # les deux, décimale en dernier
        ("1,234.5", 1234.5),
        ("'-12", -12.0),        # apostrophe de protection Excel
        ("--", None),           # mesure absente
        ("", None),
        ("abc", None),
    ],
)
def test_nombres_du_csv_et_leurs_parasites(brut, attendu):
    resultat = _nombre_csv(brut)
    if attendu is None:
        assert resultat is None
    else:
        assert resultat == pytest.approx(attendu)


@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("00:35:20", 2120.0),
        ("02:49:38", 10178.0),
        ("00:07:43.2", 463.2),
        ("35:20", 2120.0),
        ("--", None),
    ],
)
def test_durees_du_csv(brut, attendu):
    resultat = _duree_csv(brut)
    if attendu is None:
        assert resultat is None
    else:
        assert resultat == pytest.approx(attendu)


# ---- exclusion de types ----

def test_exclure_ecarte_un_type_sans_toucher_au_reste(tmp_path, db):
    # La charge d'entraînement somme toutes les activités : une discipline mal
    # mesurée par la montre pèse sur le CTL tant qu'elle est en base.
    fichier = tmp_path / "activites.csv"
    fichier.write_text(CSV_GARMIN)

    resultat = importer(fichier, db, verbose=False, exclure=("musculation",))
    assert resultat.nouvelles == 2
    assert resultat.exclues == 1
    assert all(a.type != "strength_training" for a in db.activities())


def test_exclure_accepte_le_mot_affiche_par_garmin(tmp_path, db):
    # Le type est stocké normalisé (`strength_training`), mais on écrit le mot
    # qu'on voit dans Garmin. Les deux doivent marcher, sinon l'option paraît
    # cassée alors qu'elle attend une clé interne que personne ne connaît.
    fichier = tmp_path / "activites.csv"
    fichier.write_text(CSV_GARMIN)

    for motif in ("musculation", "Musculation", "strength", "strength_training"):
        base = Database(tmp_path / f"{motif.lower()}.db")
        resultat = importer(fichier, base, verbose=False, exclure=(motif,))
        assert resultat.exclues == 1, motif


def test_sans_exclusion_tout_entre(tmp_path, db):
    fichier = tmp_path / "activites.csv"
    fichier.write_text(CSV_GARMIN)

    resultat = importer(fichier, db, verbose=False)
    assert resultat.nouvelles == 3
    assert resultat.exclues == 0


# ---- TCX ----

TCX = """<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Id>2026-08-10T05:30:00Z</Id>
      <Lap StartTime="2026-08-10T05:30:00Z">
        <TotalTimeSeconds>600.0</TotalTimeSeconds>
        <DistanceMeters>2000.0</DistanceMeters>
        <Calories>150</Calories>
        <AverageHeartRateBpm><Value>140</Value></AverageHeartRateBpm>
        <MaximumHeartRateBpm><Value>155</Value></MaximumHeartRateBpm>
      </Lap>
      <Lap StartTime="2026-08-10T05:40:00Z">
        <TotalTimeSeconds>300.0</TotalTimeSeconds>
        <DistanceMeters>1000.0</DistanceMeters>
        <Calories>90</Calories>
        <AverageHeartRateBpm><Value>170</Value></AverageHeartRateBpm>
        <MaximumHeartRateBpm><Value>180</Value></MaximumHeartRateBpm>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""


def test_tcx_reconstruit_le_resume_depuis_les_tours():
    a = lire_tcx(TCX)[0]
    assert a.type == "running"
    assert a.distance_m == pytest.approx(3000.0)
    assert a.duree_s == pytest.approx(900.0)
    assert a.fc_max == 180
    assert a.calories == 240  # 150 + 90, sommés sur les tours
    assert len(a.laps) == 2
    assert a.laps[1].allure_s_km == pytest.approx(300.0)


def test_tcx_moyenne_la_fc_ponderee_par_la_duree():
    # 140 pendant 600 s puis 170 pendant 300 s → 150, et non 155 (moyenne des
    # tours) : un tour court ne pèse pas autant qu'un tour long.
    a = lire_tcx(TCX)[0]
    assert a.fc_moy == 150


def test_tcx_convertit_l_heure_utc_en_heure_locale():
    # Un Z non converti déplacerait la séance de plusieurs heures, et parfois
    # de jour — ce qui la ferait tomber dans la mauvaise semaine de charge.
    a = lire_tcx(TCX)[0]
    assert a.debut.tzinfo is None
    attendu = datetime.fromisoformat("2026-08-10T05:30:00+00:00").astimezone()
    assert a.debut == attendu.replace(tzinfo=None)


# ---- GPX ----

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Boucle du parc</name>
    <type>running</type>
    <trkseg>
      <trkpt lat="48.8566" lon="2.3522"><time>2026-08-10T05:30:00Z</time></trkpt>
      <trkpt lat="48.8666" lon="2.3522"><time>2026-08-10T05:36:00Z</time></trkpt>
      <trkpt lat="48.8766" lon="2.3522"><time>2026-08-10T05:42:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def test_gpx_calcule_la_distance_depuis_la_trace():
    a = lire_gpx(GPX)[0]
    assert a.nom == "Boucle du parc"
    assert a.type == "running"
    # Deux fois 0,01° de latitude ≈ 2 × 1,11 km
    assert a.distance_m == pytest.approx(2224.0, rel=0.02)
    assert a.duree_s == pytest.approx(720.0)


def test_gpx_sans_horodatage_est_ignore():
    sans_temps = GPX.replace("<time>2026-08-10T05:30:00Z</time>", "").replace(
        "<time>2026-08-10T05:36:00Z</time>", ""
    ).replace("<time>2026-08-10T05:42:00Z</time>", "")
    assert lire_gpx(sans_temps) == []


# ---- dédoublonnage ----

def _act(id_: str, debut: str, laps: int = 0, fc: int | None = None) -> Activity:
    return Activity(
        activity_id=id_,
        debut=datetime.fromisoformat(debut),
        type="running",
        distance_m=10000.0,
        duree_s=3000.0,
        fc_moy=fc,
        laps=[Lap(index=i, distance_m=1000.0, duree_s=300.0) for i in range(1, laps + 1)],
    )


def test_meme_seance_par_deux_exports_ne_compte_qu_une_fois():
    # Le zip du compte et le TCX de la montre décrivent le même run sous deux
    # identifiants. Les garder tous les deux doublerait la charge du jour.
    activites = [
        _act("987654321", "2026-08-10T07:30:00", fc=148),
        _act("local-20260810T0731", "2026-08-10T07:31:00", laps=8),
    ]
    retenues = dedoublonner(activites)
    assert len(retenues) == 1
    # Le plus complet gagne : celui qui porte les tours
    assert len(retenues[0].laps) == 8


def test_deux_seances_distinctes_du_meme_jour_sont_gardees():
    # Double séance : matin et soir. Les fusionner effacerait une sortie.
    activites = [
        _act("a", "2026-08-10T07:30:00"),
        _act("b", "2026-08-10T18:30:00"),
    ]
    assert len(dedoublonner(activites)) == 2


def test_import_ignore_une_seance_deja_en_base_sous_un_autre_identifiant(db):
    db.upsert_activity(_act("987654321", "2026-08-10T07:30:00", fc=148))

    tcx = TCX.replace("2026-08-10T05:30:00Z", "2026-08-10T07:31:00")
    chemin = None
    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as d:
        chemin = P(d) / "sortie.tcx"
        chemin.write_text(tcx)
        resultat = importer(chemin, db, verbose=False)

    assert resultat.nouvelles == 0
    assert resultat.doublons == 1
    assert db.count_activities() == 1


# ---- garde-fou de vraisemblance ----

def test_activite_a_la_mauvaise_echelle_est_refusee(db, tmp_path):
    # Un export dont les unités auraient été mal reconnues : 15 km parcourus en
    # 75 secondes. Mieux vaut refuser que fausser silencieusement la charge.
    brut = json.dumps(
        [{"activityId": 7, "startTimeLocal": "2026-08-10 07:30:00",
          "activityType": {"typeKey": "running"},
          "distance": 1500000.0, "duration": 4500.0}]
    )
    fichier = tmp_path / "faux.json"
    fichier.write_text(brut)

    resultat = importer(fichier, db, verbose=False)
    assert resultat.nouvelles == 0
    assert any("invraisemblable" in raison for _, raison in resultat.ignorees)


# ---- archives et dossiers ----

def test_archive_du_compte_est_lue_et_le_reste_ignore(tmp_path, db):
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("DI_CONNECT/DI-Connect-Fitness/1_summarizedActivities.json", EXPORT_COMPTE)
        z.writestr("DI_CONNECT/DI-Connect-User/badges.json", '{"badges": []}')
        z.writestr("DI_CONNECT/reglages.txt", "sans intérêt")

    resultat = importer(archive, db, verbose=False)
    assert resultat.nouvelles == 1
    # Le .txt n'est pas un échec : il n'avait simplement rien à dire
    assert resultat.ignorees == []


def test_dossier_est_parcouru_recursivement(tmp_path, db):
    (tmp_path / "sous").mkdir()
    (tmp_path / "sous" / "a.tcx").write_text(TCX)
    (tmp_path / "b.json").write_text(EXPORT_COMPTE)

    resultat = importer(tmp_path, db, verbose=False)
    assert resultat.nouvelles == 2
    assert resultat.fichiers_lus == 2


def test_chemin_introuvable_est_signale(tmp_path, db):
    with pytest.raises(ImportError_):
        importer(tmp_path / "nexiste.pas", db, verbose=False)


def test_format_inconnu_est_ecarte_sans_interrompre(tmp_path, db):
    (tmp_path / "notes.txt").write_text("mes notes")
    (tmp_path / "vrai.json").write_text(EXPORT_COMPTE)

    resultat = importer(tmp_path, db, verbose=False)
    # Le .txt n'a même pas été proposé au lecteur : extension inconnue
    assert resultat.nouvelles == 1


def test_fichier_illisible_est_signale_mais_les_autres_passent(tmp_path, db):
    (tmp_path / "casse.tcx").write_text("<pas du xml")
    (tmp_path / "bon.json").write_text(EXPORT_COMPTE)

    resultat = importer(tmp_path, db, verbose=False)
    assert resultat.nouvelles == 1
    assert any("casse.tcx" == source for source, _ in resultat.ignorees)


def test_fit_illisible_n_interrompt_pas_le_reste_de_l_import(tmp_path, db):
    # Le .fit est une dépendance optionnelle (`pip install runningwhale[fit]`)
    # et un fichier tronqué reste possible : dans les deux cas l'import doit
    # écarter ce fichier-là, pas abandonner les autres.
    (tmp_path / "course.fit").write_bytes(b"\x0e\x10 pas un vrai FIT")
    (tmp_path / "bon.json").write_text(EXPORT_COMPTE)

    resultat = importer(tmp_path, db, verbose=False)
    assert resultat.nouvelles == 1
    assert any(source == "course.fit" for source, _ in resultat.ignorees)


def test_import_est_idempotent(tmp_path, db):
    fichier = tmp_path / "export.json"
    fichier.write_text(EXPORT_COMPTE)

    premier = importer(fichier, db, verbose=False)
    second = importer(fichier, db, verbose=False)

    assert premier.nouvelles == 1
    assert second.nouvelles == 0
    assert db.count_activities() == 1


def test_activites_entrent_de_la_plus_ancienne_a_la_plus_recente(tmp_path, db):
    # Même ordre que la synchro : si l'import meurt en route, la base ne garde
    # jamais une activité plus récente qu'une activité pas encore écrite.
    ecrites: list[datetime] = []
    original = db.upsert_activity

    def espion(activity):
        ecrites.append(activity.debut)
        return original(activity)

    db.upsert_activity = espion  # type: ignore[method-assign]

    (tmp_path / "a.tcx").write_text(TCX)
    (tmp_path / "b.json").write_text(EXPORT_COMPTE)
    importer(tmp_path, db, verbose=False)

    assert ecrites == sorted(ecrites)


def test_collecter_compte_les_fichiers_exploitables(tmp_path):
    (tmp_path / "a.json").write_text(EXPORT_COMPTE)
    (tmp_path / "vide.json").write_text("[]")

    resultat = ResultatImport()
    activites = collecter(tmp_path, resultat)
    assert len(activites) == 1
    # Un fichier sans activité ne compte pas comme lu
    assert resultat.fichiers_lus == 1
