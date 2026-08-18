# Conventions du projet

## Langue

Tout en français : code, commentaires, docstrings, messages d'erreur affichés à
l'utilisateur, documentation, messages de commit.

## Le principe qui gouverne le reste

**Une base fausse est pire qu'une base incomplète.** Ce projet préfère refuser
une donnée, ou dire qu'il ne sait pas, plutôt que d'enregistrer quelque chose de
plausible et faux. Un athlète qui voit un champ vide va le remplir ; un athlète
à qui l'on sert une valeur inventée s'entraîne de travers sans jamais savoir
pourquoi.

Les endroits qui codifient ce principe — à ne pas contourner par confort :

- `models._parse_garmin_datetime` lève plutôt que de retomber sur une date
  sentinelle, qui fausserait toutes les fenêtres de calcul.
- `imports._plausible` écarte une activité hors d'échelle (unités mal reconnues)
  et le signale.
- `imports.dedoublonner` empêche qu'une séance vue par deux exports soit comptée
  deux fois, ce qui doublerait la charge du jour.
- `analysis.allures_sous_la_marche` refuse de laisser prescrire une allure plus
  lente que la vitesse de marche de l'athlète.
- `config/athlete.example.yml` ne contient **aucune** valeur physiologique
  pré-remplie. C'est délibéré : un `fc_max: 190` d'exemple serait recopié tel
  quel par des gens qui ne l'ont jamais mesurée.

**Un diagnostic faux compte comme un bug.** Afficher « identifiants refusés »
quand la cause réelle est un pare-feu ou un filtrage d'IP envoie chercher au
mauvais endroit. Deux correctifs de `garmin._traduire` viennent de là.

## Données personnelles

Aucune donnée personnelle ne va dans le dépôt — jamais, même en test.

- Profil, base, rapports et jetons vivent dans `~/.runningwhale/`, ou dans le
  dossier pointé par `RUNNINGWHALE_HOME` — hors dépôt, même privé.
- Le `.gitignore` bloque tout `data/` sauf le `.gitkeep`.
- Les fixtures de test utilisent des valeurs inventées, un prénom neutre et
  des lieux neutres ou notoirement publics.
- Un exemple dans une docstring ne reprend pas les mesures d'une personne réelle.

## Coaching

Toute séance courue doit indiquer **la zone de fréquence cardiaque visée et sa
fourchette en bpm**, pas seulement une allure. La chaleur, le vent, le relief et
la fatigue déplacent l'allure à effort égal ; la fréquence cardiaque, non. Cette
règle est inscrite dans le prompt de construction du plan (`llm.prompt_plan_semaine`).

## Le modèle peut être la conversation

`coach plan --contexte` rend la consigne complète, et `coach plan --depuis-json`
reprend un plan produit ailleurs en lui appliquant les mêmes garde-fous que la
réponse de l'API. Quand un agent parle déjà à l'athlète, il n'a aucune raison de
repasser par un appel réseau pour produire le même JSON.

## Vérification

Avant d'affirmer qu'une chose marche : la suite complète (`python -m pytest -q`),
et pour tout ce qui touche aux données, un essai réel via la CLI — pas seulement
des tests unitaires. Un garde-fou doit être prouvé actif, pas supposé.

## Sécurité — commandements immuables pour tout développement exposé

Règles fixées par l'athlète (18/08/2026), à appliquer sans discussion dès
qu'un développement expose quelque chose au réseau — SaaS, API, site, webhook.
RunningWhale est aujourd'hui une CLI locale : les règles marquées ✔ y sont
déjà en vigueur ; les autres s'imposeront à la première brique exposée.

1. `.env` dans le `.gitignore` — ✔ ici : aucun secret suivi par git, jetons
   et clés vivent hors dépôt (`~/.runningwhale/`, variables d'environnement).
2. Clés API dans le `.env`, jamais dans le code — ✔ ici (`ANTHROPIC_API_KEY`,
   `GARMIN_TOKENS` : environnement uniquement, masquées dans les sorties).
3. Rate limiting sur la page de connexion.
4. RLS (row-level security) activée sur la base de données.
5. Mots de passe hachés, jamais lisibles — ni en base, ni en journal.
6. Droits vérifiés côté serveur, pas dans le navigateur.
7. Clé publique côté client ; la clé secrète ne quitte jamais le serveur.
8. HTTPS partout.
9. Sessions qui expirent.
10. Toute saisie utilisateur validée avant d'entrer en base — ✔ ici, c'est
    même le principe fondateur (`_plausible`, `_parse_garmin_datetime`…).
11. Taille maximale sur les fichiers uploadés.
12. Type des fichiers uploadés vérifié aussi.
13. CORS configuré explicitement.
14. Messages d'erreur détaillés coupés en production — ✔ ici : pas de
    traceback brut, secrets masqués (`garmin.masquer_secrets`).
15. Journaux propres, non verbeux (pas de `console.log` de debug qui traîne).
16. Un seul message d'erreur indistinct pour email et mot de passe — ne pas
    révéler lequel des deux est faux. Nuance assumée : ne jamais non plus
    accuser les identifiants quand la vraie cause est ailleurs (voir « un
    diagnostic faux compte comme un bug »).
17. Webhooks protégés par une signature vérifiée.
18. Dépendances à jour.
19. Confirmation par email à l'inscription.
20. Sauvegarde automatique et récurrente de la base de données.

## Synchronisation en conversation

À chaque échange avec le coach, commencer par `coach sync --espacer 3` : la
synchro n'a lieu que si la dernière date de plus de 3 heures, sinon la commande
ne fait rien et le dit. Inutile de marteler Garmin à chaque message — et ne
jamais laisser croire qu'une synchro a eu lieu quand elle a été sautée.
