# RunningWhale 🐋

Un coach de course à pied qui travaille sur **tes** données, pas sur des
moyennes de magazine. Tu lui donnes l'historique de tes sorties, il calcule ta
charge d'entraînement, ta forme du moment et tes allures de référence — puis
**Claude** t'explique ce que ça veut dire et construit ta semaine.

Tout tourne sur ta machine. Rien n'est publié, rien n'est envoyé à un service
tiers.

```
coach init        # 3 minutes de questions, une seule fois
coach import      # tes sorties, depuis l'export de ta montre
coach status      # ton tableau de bord
coach plan        # ta semaine d'entraînement
coach debrief     # l'analyse de ta dernière séance
```

## Pour qui

Pour quelqu'un qui court déjà un peu et qui aimerait comprendre ce qu'il fait.
Tu n'as besoin d'aucune connaissance en physiologie : le coach explique chaque
indicateur en français, et te dit quand il n'est pas fiable.

Tu n'as pas besoin d'être rapide non plus. Le coach s'adapte à ton niveau — il
vérifie même qu'il ne te propose pas des allures plus lentes que ta vitesse de
marche, ce qui arrive plus souvent qu'on ne croit quand on débute.

## Démarrage — 10 minutes en tout

### 1. Installer

```bash
git clone https://github.com/NaoVicarius/RunningWhale.git
cd RunningWhale
pip install -e .
```

Python 3.10 ou plus.

### 2. Créer ton profil

```bash
coach init
```

Il te pose **quatre questions** : ton année de naissance, ta taille, ton poids
et tes contraintes physiques (blessures passées, articulations fragiles).

Tu peux passer n'importe laquelle en appuyant sur Entrée. Une seule règle :

> **Ne remplis que ce que tu sais.**
> Un champ vide n'est jamais un problème, le coach s'adapte et te le dit.
> Une valeur inventée, elle, fausse tout en silence.

### 3. Récupérer tes courses ⭐

C'est **l'étape indispensable** : sans tes sorties, le coach n'a rien à
analyser. Compte deux minutes.

1. Va sur [connect.garmin.com](https://connect.garmin.com) **depuis un
   ordinateur** (l'export n'existe pas sur mobile).
2. Menu de gauche → **Activités** → **Toutes les activités**.
3. **Fais défiler jusqu'en bas** pour charger tout ton historique. La page en
   charge un morceau à la fois : plus tu descends, plus tu en récupéreras.
   Vise au moins trois mois, un an c'est mieux.
4. En haut à droite, clique **Exporter CSV**.
5. Tu obtiens un fichier `Activities.csv` dans tes téléchargements.

Puis :

```bash
coach import ~/Téléchargements/Activities.csv
```

**Tu n'as pas de Garmin ?** Le coach lit aussi les fichiers `.tcx`, `.fit` et
`.gpx` que produisent la plupart des montres et applications (Strava, Polar,
Suunto, Coros…). Exporte tes activités et donne-lui le dossier :

```bash
coach import ~/Téléchargements/
```

Il fouille récursivement et ne compte jamais deux fois la même sortie, même si
elle arrive sous deux formats différents.

### 4. Regarder

```bash
coach status
```

Ton tableau de bord, calculé localement, sans aucun appel à un modèle.

## En option : ta balance connectée

Si tu as une balance à impédance (Renpho, Withings, Garmin Index…), une pesée
aide le coach à ajuster ses conseils — surtout la vitesse à laquelle tu peux
monter en volume.

```bash
coach poids --kg 74.2 --graisse 18.5 --muscle 58.1
```

Toutes les options sont facultatives sauf le poids. `coach poids --help` les
liste.

Le plus simple, si tu utilises le coach depuis Claude : **envoie-lui la capture
d'écran de ton appli de balance**, il saisira les valeurs pour toi.

Pèse-toi plusieurs fois plutôt qu'une : le coach travaille sur la **tendance**,
pas sur la mesure du jour. Entre l'hydratation, le repas et l'heure de la pesée,
un chiffre isolé varie de plus d'un kilo et ne dit presque rien.

## Ce qu'il calcule

Tout est dérivé de tes activités, sans service tiers :

| Indicateur | Ce qu'il te dit |
|---|---|
| **CTL / ATL / TSB** | ta condition de fond, ta fatigue récente, la fraîcheur qui reste |
| **ACWR** | si ta montée de charge reste dans la zone sûre (0,8 – 1,3) |
| **Zones de FC** | tes cinq zones en battements par minute, directement pilotables |
| **Répartition d'intensité** | la part de facile / modéré / dur |
| **Dérive cardiaque** | la perte d'efficacité entre la première et la seconde moitié d'une sortie |
| **VMA et allures** | tes allures de référence, de l'endurance à la VMA |
| **Projections** | tes temps prévisibles sur 5 km, 10 km, semi et marathon |
| **Composition** | poids, masse grasse, masse maigre et leur tendance |
| **Adhérence au plan** | ce qui était prévu face à ce que tu as réellement couru |

La charge d'une séance utilise le **TRIMP de Banister** quand la fréquence
cardiaque est disponible, retombe sur un calcul à partir de l'allure sinon, et
sur la durée seule en dernier recours — pour rester utilisable quelle que soit
ta montre.

## Où sont tes données

**Rien de personnel ne vit dans ce dépôt.** Tout est rangé dans un dossier privé
sur ta machine :

```
~/.runningwhale/
├── athlete.yml          ton profil (âge, taille, poids, contraintes, objectifs)
├── runningwhale.db      tes sorties, tes pesées, tes rapports
├── reports/             les plans et débriefs générés
└── garmin_tokens/       jetons de connexion, si tu utilises coach login
```

Le dépôt ne contient que du code. Le `.gitignore` bloque explicitement toute
donnée personnelle : même en essayant, tu ne peux pas la publier par accident.

Tu peux déplacer ce dossier avec la variable `RUNNINGWHALE_HOME` :

```bash
export RUNNINGWHALE_HOME=~/Documents/course
```

Pour tout effacer, il suffit de supprimer `~/.runningwhale/`.

## Le coaching : deux façons

Les calculs (`coach status`, `coach import`, `coach poids`) sont **100 % locaux**
et gratuits. Seules l'analyse rédigée et la construction du plan font appel à
Claude, et tu as le choix :

**Depuis Claude Code ou Claude Desktop** — sans clé API, sans frais
supplémentaires. Tu discutes avec le coach, il lit tes chiffres et écrit ton
plan. Voir la section MCP plus bas, ou simplement :

```bash
coach plan --contexte          # affiche tout ce que le coach doit savoir
# tu colles ça dans Claude, il te rend un plan en JSON
coach plan --depuis-json plan.json
```

Le plan produit ainsi passe exactement les mêmes contrôles de sécurité que
celui de l'API : progression de charge, cohérence des allures, séances dures
trop rapprochées.

**Avec une clé API Anthropic** — pour que `coach plan`, `coach debrief` et
`coach ask` fonctionnent tout seuls, en tâche planifiée par exemple :

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Commandes

| Commande | Ce qu'elle fait |
|---|---|
| `coach init` | crée ton profil et tes dossiers (questionnaire) |
| `coach import <chemin>` | **lit tes exports de montre** (CSV, TCX, FIT, GPX, zip) |
| `coach poids --kg …` | enregistre une pesée et suit la tendance |
| `coach login` | connecte le compte Garmin |
| `coach sync` | récupère les nouvelles activités (incrémental) |
| `coach status` | tableau de bord local — **aucun appel au modèle, gratuit** |
| `coach bilan` | tableau de bord + analyse du coach |
| `coach debrief [id]` | débriefe une séance (la dernière par défaut) |
| `coach plan` | construit le plan de la semaine |
| `coach ask "…"` | pose une question, avec tes données en contexte |
| `coach watch` | synchronise et débriefe les nouvelles séances |
| `coach cron` | affiche la ligne de crontab à installer |
| `coach-mcp` | lance le serveur MCP (voir plus bas) |

Les rapports sont écrits en Markdown dans `~/.runningwhale/reports/` et
conservés en base pour l'historique.

`coach status --json` renvoie les mêmes données sans mise en forme, pour un
script ou un agent :

```bash
coach status --json | jq '.forme, .recuperation.alertes'
```

`coach sync` récupère aussi le sommeil, la VFC et la readiness par défaut
(`--sans-recup` pour l'ignorer, `--jours-recup N` pour la profondeur).

### Le plan de la semaine

Le plan n'est pas de la prose : le modèle le produit en **JSON contraint par un
schéma**, ce qui permet de l'exporter et de le suivre.

```bash
coach plan                       # génère et affiche le plan
coach plan --ics                 # exporte aussi un calendrier .ics
coach plan --push                # téléverse les séances dans Garmin Connect
coach plan --consignes "je pars en déplacement jeudi et vendredi"
coach plan --montrer             # réaffiche le plan en cours
```

Avec `--push`, chaque séance devient une séance structurée programmée dans ton
calendrier Garmin, avec ses blocs et ses **cibles d'allure** — elle apparaît
directement sur la montre le jour venu.

À la génération suivante, le coach voit ce que tu as réellement couru face à ce
qui était prévu, et en tient compte.

## Parler à ton coach depuis Claude (MCP)

Le CLI suppose un terminal. Le **serveur MCP** expose les mêmes données à Claude
Desktop, claude.ai ou Claude Code, pour discuter en langage naturel :

> *« je me sens cassé cette semaine, je maintiens ma sortie longue ? »*

Le modèle appelle alors `etat_de_forme` et `recuperation`, voit ta fraîcheur
réelle et tes alertes, et répond avec **tes** chiffres plutôt qu'au jugé.

```bash
pip install -e ".[mcp]"
```

Puis, dans la configuration MCP de ton client (`claude_desktop_config.json`, ou
`.mcp.json` pour Claude Code) :

```json
{
  "mcpServers": {
    "runningwhale": {
      "command": "coach-mcp",
      "env": {
        "RUNNINGWHALE_CONFIG": "/chemin/vers/config/athlete.yml"
      }
    }
  }
}
```

> `RUNNINGWHALE_CONFIG` n'est pas facultatif : le client lance le serveur depuis
> un répertoire de travail arbitraire, où un chemin relatif ne veut rien dire.
> Sans elle, le coach tournerait sur un profil vide et des calculs faux.

### Les outils exposés

| Outil | Ce qu'il renvoie |
|---|---|
| `etat_de_forme` | CTL, ATL, TSB, ACWR, alertes de récupération, objectif |
| `bilan_complet` | l'ensemble des métriques |
| `recuperation` | sommeil, VFC, FC de repos et readiness, jour par jour |
| `activites_recentes` | les dernières sorties avec allure, FC et charge |
| `detail_seance` | tours, dérive cardiaque, gestion d'allure d'une séance |
| `allures` | allures d'entraînement et projections de temps |
| `plan_en_cours` | le plan et son taux de réalisation |
| `synchroniser` | déclenche une synchro Garmin |
| `enregistrer_plan` | sauvegarde un plan conçu dans la conversation |

**Le serveur n'appelle aucun modèle** — le client connecté *est* le modèle. Il
expose des données et des calculs déjà faits, pour que le modèle raisonne sur des
chiffres justes au lieu d'agréger des activités de tête. Aucune clé
`ANTHROPIC_API_KEY` n'est nécessaire pour cet usage.

`enregistrer_plan` referme la boucle : un plan conçu dans la conversation devient
le plan courant, donc exportable en `.ics` ou poussé vers la montre avec
`coach plan --ics` et `--push`.

### Et les serveurs MCP Garmin existants ?

Il en existe plusieurs, mais ils enveloppent la même bibliothèque `garminconnect`
que nous utilisons déjà. Les brancher pour la **synchro** serait le mauvais outil :
récupérer et stocker des activités est un traitement déterministe, que faire
transiter par un modèle rendrait plus lent, plus cher et non reproductible. Même
chose pour les calculs — CTL, ACWR et Riegel sont des mathématiques, pas du
jugement. D'où ce partage : **Python calcule, Claude interprète.**

## Automatisation

`coach watch` synchronise puis débriefe toute nouvelle séance. Une séance n'est
jamais débriefée deux fois.

```bash
coach cron          # affiche la ligne à coller dans `crontab -e`
```

La clé API est lue depuis `~/.runningwhale/env`, un fichier restreint à ton
compte — jamais écrite dans la crontab elle-même, où elle serait lisible via
`crontab -l` et visible dans `ps` pendant l'exécution :

```bash
printf 'ANTHROPIC_API_KEY=%s\n' "sk-ant-..." > ~/.runningwhale/env
chmod 600 ~/.runningwhale/env
```

Puis, tous les soirs à 21h30 :

```cron
30 21 * * * . ~/.runningwhale/env && /usr/local/bin/coach watch >> ~/.runningwhale/watch.log 2>&1
```

Pour systemd, deux unités prêtes à l'emploi sont fournies dans `scripts/` :

```bash
cp scripts/runningwhale.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now runningwhale.timer
```

## Coût

`coach status` est entièrement local et gratuit. Seules les commandes qui
appellent le modèle (`bilan`, `debrief`, `plan`, `ask`, `watch`) sont facturées,
sur ta clé API Anthropic. Le prompt système est mis en cache, et le contexte
envoyé est compact : il contient des métriques agrégées, pas le détail brut de
toutes tes sorties.

Tu peux réduire le coût en abaissant `coach.effort` dans la configuration
(`low`, `medium`, `high`, `xhigh`, `max`).

## Vie privée

En usage local, tout reste local : la base SQLite, les jetons Garmin et les
rapports vivent dans `~/.runningwhale/`, restreint à ton compte (`0700`, base et
jetons en `0600`).
Rien n'est envoyé ailleurs que vers Garmin Connect (pour lire tes activités) et
l'API Anthropic (pour l'analyse).

La base conserve le JSON brut de chaque activité et de chaque journée de
récupération, pour pouvoir corriger l'extraction a posteriori. Les champs les
plus sensibles en sont élagués avant stockage : coordonnées GPS de départ et
d'arrivée (qui désignent le domicile pour une sortie régulière) et identité du
compte Garmin. Le reste — fréquence cardiaque, sommeil, identifiants d'appareil —
demeure, localement.

## Développement

```bash
pip install -e ".[dev]"
pytest
```

La suite couvre les calculs d'entraînement, la récupération, le plan structuré,
le rendu des rapports, les outils MCP et la construction des requêtes au modèle
(avec un client simulé — aucun test ne fait d'appel réseau).

```
runningwhale/
├── config.py     profil athlète, objectifs, chemins
├── garmin.py     connexion et synchronisation incrémentale
├── db.py         stockage SQLite
├── models.py     représentation normalisée d'une activité
├── wellness.py   sommeil, VFC, readiness — extraction tolérante
├── analysis.py   charge, forme, récupération, allures, projections
├── plan.py       plan structuré, export ICS, séances Garmin
├── llm.py        construction du contexte et appels à Claude
├── report.py     rendu Markdown et sérialisation JSON
├── mcp_server.py serveur MCP (aucun appel au modèle)
└── cli.py        interface en ligne de commande
```

## Avertissement

RunningWhale est un outil d'aide à l'entraînement, pas un avis médical. Une
douleur qui persiste, un symptôme inhabituel ou une fatigue anormale relèvent
d'un professionnel de santé.

Projet indépendant, sans affiliation avec Garmin ni Anthropic.

## Licence

RunningWhale est distribué sous licence [GNU AGPL v3](LICENSE)
(© 2026 NaoVicarius). Vous pouvez l'utiliser, le modifier et le redistribuer
librement ; si vous le proposez comme service accessible par le réseau, la
licence vous engage à publier vos modifications sous les mêmes termes.
