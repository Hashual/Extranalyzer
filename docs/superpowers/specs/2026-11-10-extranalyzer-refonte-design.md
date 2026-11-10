# Extranalyzer — refonte complète (design)

Date : 2026-11-10 · Statut : **validé par l'utilisateur** (2026-11-10)

## 1. Intention

Donner à l'utilisateur une vue d'ensemble fiable de ses dépenses **sur une période qu'il choisit**,
afin de repérer où réduire, optimiser ou augmenter ses coûts.

L'outil actuel (scripts Python → un Excel par année) ne permet pas cela :
- les montants ne sont pas signés : un virement reçu est compté comme un débit ;
- des transactions fusionnent entre elles ;
- environ un tiers des lignes reste « OTHER » ;
- aucune période libre n'est possible.

Le texte des PDF n'est pas stocké dans l'ordre visuel : les montants sont stockés en bloc, séparés des
libellés. Il faut donc reconstruire les lignes à partir des **coordonnées** des mots.

On repart de zéro. Seuls les relevés PDF existants (`extraits/`) sont conservés comme matière première
et comme jeu de test.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Extraire chaque transaction avec date, libellé complet, montant **signé** (débit/crédit) et compte.
- Classer chaque transaction en **Catégorie / Sous-catégorie** (deux niveaux).
- Un panneau web pour :
  - déposer les relevés mensuels ;
  - consulter une période libre, avec comparaison à une autre période ;
  - trancher les libellés inconnus.
- L'épargne est suivie à part, par destination (ex. Épargne / Livret Bleu, Épargne / Kraken).
- Les onglets de l'interface sont modulaires : en ajouter ou en retirer est trivial.
- Hébergement sur Raspberry Pi via Docker, avec une image la plus petite et la plus sobre possible.

**Non-objectifs**
- Aucune connexion aux API bancaires ni synchronisation automatique : le dépôt du PDF reste manuel.
- Pas de multi-utilisateurs ni de gestion de comptes.
- Pas de budget prévisionnel ni d'alertes (ajoutables plus tard sous forme d'onglet).
- Pas de prise en charge d'autres banques au départ. Le format du parseur est isolé pour pouvoir en ajouter.
- Pas encore de sources externes (portefeuilles crypto via Solscan & co, plateformes comme Kraken), ni de
  vue des actifs détenus. **Prévu plus tard** : l'architecture doit le permettre sans refonte (§ 5.10).

**Anti-objectifs (échec même si « ça marche »)**
- **Chiffres faux importés en silence.** Un relevé dont les soldes ne se réconcilient pas ne doit **jamais**
  être intégré.
- **Classement deviné sans le dire.** Un libellé non couvert par une règle va dans « À classer », jamais
  dans une catégorie par défaut.
- **Épargne gonflant les dépenses.** Un virement vers un compte d'épargne n'est pas une dépense, et un
  virement interne n'est pas compté deux fois.
- **Panneau exposé sur internet.**

## 3. Contraintes

- **Confidentialité.**
  - Les données restent sur le Raspberry.
  - Aucune ressource n'est chargée depuis internet par l'interface : bibliothèques embarquées.
  - Aucune télémétrie.
  - Les jeux de test commités et les maquettes ne contiennent **aucune donnée réelle** des relevés
    (données fictives uniquement).
  - Règle de travail pour l'assistant : ne jamais garder en mémoire persistante d'informations issues
    des relevés.
- **Réseau.** Accès uniquement via le VPN WireGuard de l'utilisateur (et le réseau local). Le panneau
  n'est pas publié sur internet. Un mot de passe applicatif simple s'ajoute par défense en profondeur.
- **Matériel.** Raspberry Pi, ARM64 (`linux/arm64`), ressources limitées.
- **Format source.** Relevés « RELEVE ET INFORMATIONS BANCAIRES » de la banque actuelle :
  - comptes vus : compte courant et Livret Bleu ;
  - colonnes « Débit EUROS » et « Crédit EUROS » ;
  - montants au format `1.234,56` ;
  - soldes d'ouverture et de clôture : « SOLDE CREDITEUR/DEBITEUR AU jj/mm/aaaa ».

## 4. Approches envisagées

| Approche | Pour | Contre |
|---|---|---|
| Rust, binaire unique avec serveur web | binaire léger, faible RAM, typage strict | outillage PDF moins mûr : `pdfium-render` nécessite une lib native ; développement plus long |
| Rust, ligne de commande + HTML statique | très simple | pas de dépôt via le web, comparaison de périodes peu pratique |
| **Python + Docker** (retenu) | `pdfplumber` donne directement les mots avec coordonnées ; itération rapide sur le parseur ; volume minuscule, donc la performance est sans enjeu | nécessite un conteneur : à optimiser en taille et en RAM |

**Choix de l'utilisateur :** Python + Docker, avec la consigne d'optimiser le compose pour obtenir les plus
petites images possibles, en taille comme en consommation.

## 5. Décisions de conception

### 5.1 Pile technique
- Python 3.12, FastAPI + Uvicorn (1 worker), `pdfplumber` pour l'extraction, SQLite (module standard
  `sqlite3`).
- Interface : HTML + JavaScript « vanille » en modules ES, **sans étape de build**. Chart.js est embarqué
  dans le dépôt (vendored) et servi en statique.

### 5.2 Architecture modulaire

```
app/
├─ main.py            # assemble l'appli : charge les modules d'onglets déclarés
├─ config.py          # variables d'env (mot de passe, chemins, limites)
├─ auth.py            # mot de passe -> cookie de session signé
├─ importer/
│  ├─ base.py         # interface StatementParser (format bancaire interchangeable)
│  ├─ creditmutuel.py # parseur par coordonnées
│  └─ validate.py     # réconciliation des soldes
├─ classify/
│  ├─ rules.py        # moteur de règles
│  └─ transfers.py    # appariement des virements internes
├─ store/             # schéma SQLite + migrations simples + requêtes
├─ tabs/              # UN module par onglet (côté serveur)
│  ├─ __init__.py     # registre : liste ordonnée des onglets actifs
│  ├─ overview.py  categories.py  transactions.py
│  ├─ epargne.py   a_classer.py   releves.py  regles.py
└─ web/
   ├─ index.html  app.js  style.css  vendor/chart.umd.min.js
   └─ tabs/           # UN module JS par onglet (même nom que côté serveur)
```

**Contrat d'un onglet**
- Côté serveur : un module Python qui expose `TAB = Tab(id, titre, ordre, router)`, où `router` est un
  `APIRouter` monté sous `/api/tabs/<id>/`.
- Côté client : `web/tabs/<id>.js` exporte `{ id, title, render(container, ctx) }`. `ctx` fournit la
  période sélectionnée, la période de comparaison et un client API.
- Le registre (`tabs/__init__.py`) est **la seule liste à modifier** :
  - le serveur l'expose via `/api/tabs` ;
  - le client construit la barre d'onglets et importe dynamiquement chaque module JS.
- Le cœur (import, classement, stockage) ne connaît pas les onglets.

### 5.3 Import et parseur
- **Dépôt** depuis l'interface. Contrôles :
  - fichier PDF uniquement, taille maximale configurable ;
  - empreinte SHA-256 : un fichier déjà importé est ignoré ;
  - un relevé déjà importé (même compte et même date) est signalé.
- **Conservation** : les PDF originaux sont gardés dans le volume de données, pour pouvoir **ré-analyser**
  tout l'historique quand le parseur ou les règles évoluent (bouton dans l'onglet Relevés).
- **Un PDF contient plusieurs comptes** (vérifié sur les relevés réels : jusqu'à 4 comptes — compte
  courant, Livret Bleu, Livret Jeune, CEL — chacun avec ses propres soldes ; l'en-tête peut se répéter
  d'une page à l'autre). Modèle : **1 PDF → N sections**, une par compte ; chaque section est un
  « relevé de compte » avec ses soldes et sa réconciliation propres. Le bloc « SITUATION DE VOS AUTRES
  COMPTES » (soldes seuls, sans mouvements) n'est pas une section.
- **Pas de débit différé** (vérifié : aucun « FACTURE CARTE ») : les paiements carte sont listés un à un.
- **Algorithme par section** (via `pdfplumber`, mots avec coordonnées x/y) :
  1. Identifier la date du relevé (ex. « 10 avril 2024 ») puis découper en sections à chaque en-tête de
     compte (intitulé + numéro) ; une section se poursuit sur la page suivante si l'en-tête y est répété.
  2. Repérer les abscisses des en-têtes « Débit EUROS » et « Crédit EUROS » pour définir les deux colonnes
     de montants.
  3. Grouper les mots en lignes visuelles par ordonnée (avec une tolérance).
  4. Une ligne commençant par deux dates `jj/mm/aaaa` ouvre une transaction. Les lignes suivantes sans
     date et sans montant sont des **continuations du libellé**.
  5. Le montant d'une transaction est celui aligné verticalement sur sa ligne. Sa colonne (débit ou
     crédit) donne son signe.
  6. Lire les « SOLDE CREDITEUR/DEBITEUR AU … » d'ouverture et de clôture.
  7. Arrêter la lecture à la fin du tableau : bloc « SITUATION DE VOS AUTRES COMPTES », mentions de pied
     de page, « Page N ».
- **Réconciliation (bloquante, par section)** : `ouverture − Σdébits + Σcrédits = clôture` au centime près.
  - En cas d'échec, **seule cette section** est marquée **en erreur** dans l'onglet Relevés, avec l'écart ;
    aucune de ses transactions n'est intégrée. Les autres sections du PDF sont importées. *(choix du
    concepteur)*
  - Les relevés de plusieurs pages et ceux sans mouvement doivent passer.
- **Relevé déjà importé** (même compte, même date) : contenu identique → ignoré ; contenu différent →
  l'interface propose de remplacer l'ancienne version. *(choix du concepteur)*
- **Ré-analyse** : identité stable d'une transaction = (compte, date d'opération, montant signé, libellé
  normalisé, rang parmi les doublons exacts du même relevé). Les reclassements manuels sont rattachés à
  cette identité et ré-appliqués. Si une section qui se réconciliait échoue après ré-analyse, l'ancienne
  version est conservée et la section est signalée. *(choix du concepteur)*
- **Date retenue** : la date d'opération. La date de valeur est stockée sans être utilisée pour l'analyse.

### 5.4 Classement
- **Hiérarchie à deux niveaux** : Catégorie / Sous-catégorie. Liste initiale proposée par le builder,
  puis **validée avec l'utilisateur** (voir § 7).
- **Règles** stockées en base, éditables dans l'onglet Règles :
  - motif (contient / commence par / regex), sens optionnel (débit/crédit), compte optionnel →
    catégorie / sous-catégorie ;
  - priorité explicite (la plus haute gagne), **préremplie selon la spécificité** du motif à la création
    et modifiable *(choix du concepteur)* ;
  - chaque transaction mémorise la règle qui l'a classée ; modifier ou supprimer une règle reclasse les
    transactions concernées, **sauf** celles reclassées à la main *(choix du concepteur)* ;
  - un fichier `rules.seed.toml` peut initialiser la base.
- **Inconnus** : toute transaction sans règle correspondante va dans « À classer ».
  - L'utilisateur choisit catégorie et sous-catégorie, et peut créer une règle en un clic. Le motif proposé
    est le libellé nettoyé : sans dates, numéros de carte ni références.
  - La nouvelle règle s'applique rétroactivement aux transactions encore à classer.
- **Surcharge manuelle** : une transaction reclassée à la main garde ce choix (`manual=1`). Les règles ne
  l'écrasent plus.
- **Revenus** : les crédits classés dans une catégorie de revenus (ex. Revenus / Salaire) forment les
  revenus.

### 5.4 bis Définition des totaux (validée par l'utilisateur)
- **Remboursements** : un crédit classé dans une catégorie de dépense (ex. Courses) **réduit** cette
  catégorie ; il ne compte pas comme revenu. Les catégories sont donc des montants **nets**.
- **Dépenses** = Σ des catégories de dépense (nettes). **Revenus** = Σ des catégories de revenus.
- **Épargne** = **nette** : versements − retraits, toutes destinations ; le détail versements / retraits
  est dans l'onglet Épargne.
- **Intérêts des livrets** : classés **Épargne / Intérêts** (pas en revenus).
- **Solde de la période** = Revenus − Dépenses − Épargne nette.

### 5.5 Épargne et virements internes
- **Catégorie « Épargne »**, avec la destination en sous-catégorie : Livret Bleu, Kraken, etc.
  - Elle est **exclue du total des dépenses** et affichée dans son propre encart et son propre onglet.
  - Un retrait d'épargne vers le compte courant n'est pas un revenu.
- **Comptes de l'utilisateur** : comptes connus (courant, Livret Bleu…) détectés par leurs relevés.
- **Appariement** : un virement apparaissant sur deux relevés de l'utilisateur (même montant opposé,
  dates proches, libellés compatibles) est apparié et compté **une seule fois**.
- **Vue par défaut** : les dépenses et revenus portent sur le compte courant. Le sélecteur de comptes
  permet d'élargir.

### 5.6 Interface (validée sur maquettes)
- **Barre commune** : période (date de début → date de fin, plus des raccourcis mois, trimestre et
  année), période de comparaison, bouton « Déposer un relevé », pastille « À classer (n) ».
- **Onglets initiaux** :
  - **Vue d'ensemble** : totaux Dépenses / Revenus / Épargne / Solde de la période avec la variation par
    rapport à la période de comparaison ; camembert de répartition ; barres par catégorie (un clic ouvre
    l'onglet Catégories) ; évolution mois par mois en barres empilées avec une courbe d'épargne ; plus
    grosses hausses ; dépenses récurrentes détectées.
  - **Catégories** : liste des catégories, puis le détail d'une catégorie (sous-catégories, courbe,
    transactions, reclassement sur place).
  - **Transactions** : tableau filtrable et triable.
  - **Épargne** : suivi par destination.
  - **À classer**.
  - **Relevés** : liste, statut de réconciliation, ré-analyse.
  - **Règles**.
- **Dépenses récurrentes** : même libellé normalisé et montant proche, sur au moins 3 mois consécutifs.

### 5.7 Sécurité
- Le conteneur publie le port **uniquement sur l'adresse WireGuard et/ou LAN** du Pi (`BIND_ADDR`
  configurable dans le compose), jamais sur `0.0.0.0` par défaut.
- **Mot de passe** :
  - fourni par variable d'environnement ou secret Docker (haché au démarrage) ;
  - session par cookie signé, `HttpOnly`, `SameSite=Strict` ;
  - limitation des tentatives de connexion.
- Protection CSRF sur les requêtes qui modifient des données (le cookie `SameSite=Strict` et un en-tête
  personnalisé suffisent).
- Les uploads sont traités comme non fiables : taille limitée, type vérifié, analyse dans le processus
  sans exécuter quoi que ce soit.

### 5.8 Docker : taille et sobriété
- **Un seul service** (application + SQLite dans le même processus), sans conteneur de base séparé.
- **Dockerfile en deux étapes** :
  - étape de construction : on prépare les wheels et le venv ;
  - étape finale minimale : pas de compilateur, pas de cache pip, pas de fichiers `.pyc` superflus,
    utilisateur non-root.
- **Base** : `python:3.12-alpine` (cible `linux/arm64`).
  - Le builder vérifie que `pdfplumber` et ses dépendances (`pdfminer.six`, `pypdfium2`, `Pillow`) ont des
    wheels musllinux ARM64.
  - À défaut, repli sur `python:3.12-slim`, et la taille finale obtenue est consignée.
- **Compose** :
  - `read_only: true`, un volume nommé pour `/data` (SQLite + PDF), `tmpfs` pour `/tmp` ;
  - `mem_limit` (≈ 256 Mo) et `cpus` (≈ 1.0) ;
  - `restart: unless-stopped` ;
  - `healthcheck` léger : Python standard, sans curl ;
  - `security_opt: no-new-privileges`, `cap_drop: ALL`.
- **Build multi-architecture** possible depuis le PC (`docker buildx --platform linux/arm64`) ou
  directement sur le Pi.

### 5.10 Extension future : sources externes et actifs (hors périmètre, à ne pas bloquer)
L'utilisateur veut plus tard ajouter ses comptes crypto (adresses publiques via Solscan ou équivalents) et
des plateformes comme Kraken, pour voir aussi les **actifs détenus** (positions, valorisation). Pour ne pas
fermer cette porte, sans rien implémenter de plus maintenant :
- Le module d'import est conçu autour d'une notion générale de **source** : `importer/base.py` définit
  `Source` dont « relevé PDF déposé » n'est qu'une implémentation. Une future source « API » (Kraken,
  Solscan…) produira les mêmes objets (comptes, transactions) et pourra en plus produire des **positions**.
- Le schéma prévoit une table `accounts` avec un `kind` (bank, exchange, wallet…) et une `source` ; les
  positions/valorisations seront des tables séparées ajoutées le moment venu, avec un onglet
  « Patrimoine » branché via le registre d'onglets.
- Les virements vers Kraken & co restent pour l'instant de l'**Épargne / Kraken** côté compte courant.
- Contrainte anticipée : clés d'API en **lecture seule**, stockées comme secrets Docker, jamais en base ni
  dans le dépôt ; le conteneur aura alors besoin d'un accès sortant vers ces API uniquement.

### 5.9 Tests
- **Parseur** : tests sur des PDF **synthétiques** générés pour le dépôt, reproduisant la mise en page
  (colonnes, libellés multi-lignes, plusieurs pages, solde débiteur, relevé vide). Aucun relevé réel n'est
  commité.
- **Vérification locale non commitée** : un script lance le parseur sur `extraits/` et rapporte seulement
  le nombre de relevés réconciliés ou en échec, sans afficher de contenu.
- **Tests unitaires** : règles, appariement, totaux par période, et registre d'onglets (ajout et retrait
  d'un onglet factice).

## 6. Laissé au builder
- Structure exacte du schéma SQLite et des migrations.
- Tolérances numériques du regroupement en lignes.
- Normalisation des libellés : retrait des numéros de carte, des dates `jjmm` et des références.
- Style visuel de l'interface (sobre, avec thème clair et sombre).
- Détails de l'appariement : fenêtre de dates, score de similarité.

## 7. Points à trancher avec l'utilisateur pendant la réalisation
- **Liste initiale des catégories et sous-catégories** : le builder propose, l'utilisateur valide.
- **Libellés ambigus** rencontrés lors du premier import complet : l'utilisateur tranche via l'onglet
  « À classer », sans que le builder devine.
