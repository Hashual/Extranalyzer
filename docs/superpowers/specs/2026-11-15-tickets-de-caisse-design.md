# Extranalyzer : tickets de caisse et détail des courses par rayon (design)

Date : 2026-11-15 · Statut : **validé oralement par l'utilisateur (2026-11-15), relecture du document en attente**

## 1. Intention

L'utilisateur veut savoir **ce qu'il consomme, rayon par rayon**. Aujourd'hui, un passage en caisse
n'apparaît que sous la forme d'une ligne carte classée « Courses ». Le détail est pourtant disponible :
il reçoit par email un ticket de caisse dématérialisé pour chaque achat.

L'application doit :

- récupérer ces tickets ;
- relier chaque ticket au débit carte correspondant, quand c'est possible ;
- ranger chaque article par rayon, puis par famille de rayons ;
- montrer, sur la période choisie, combien il dépense par famille, par rayon et par produit.

## 2. État des lieux (constaté le 2026-11-15)

- **Super U**, enseigne habituelle jusqu'en août 2026 : 99 tickets dans un compte Gmail dédié, sous le
  libellé `Super U/Tickets de caisses`.
  - Expéditeur : Coopérative U (domaine `cooperative-u.fr`).
  - Le corps du mail indique le magasin et le montant total.
  - Chaque mail a deux PDF joints : `Ticket de caisse_JJMMAAAA-HHMMSS.pdf` et
    `Ticket de CB_JJMMAAAA-HHMMSS.pdf`.
  - Le ticket de caisse est un PDF texte (pas une image), sur une seule page longue.
  - Il est **déjà découpé par rayon** : un titre souligné en gras (`BEURRE`, `BISCUITS SUCRES`,
    `CAFE TORREFIE/SOLUBLE`, `FROMAGE LS`, `FRUITS`, `OEUFS`, `ULTRA FRAIS`…), puis les articles de ce
    rayon.
  - Chaque article tient sur deux lignes : `LIBELLÉ … total € code TVA`, puis `n x prix unitaire EUR`.
  - Suivent ensuite les totaux, les moyens de paiement, la fidélité et un code-barres.
- **Intermarché**, enseigne actuelle : aucun ticket reçu pour l'instant. Ils arriveront dans le même
  compte Gmail, sous le libellé `intermarche/Tickets de caisses`. Leur format est inconnu.

## 3. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Récupérer **automatiquement** les tickets dans le compte Gmail : lecture seule, uniquement les deux
  libellés ci-dessus. Le **dépôt manuel** d'un ou plusieurs PDF reste possible en secours.
- Lire les tickets Super U : magasin, date et heure, articles (rayon, libellé, quantité, prix unitaire,
  total), total du ticket, montant payé par carte.
- Relier un ticket à son débit carte. Automatiquement quand il n'y a qu'un seul candidat, à la main
  sinon.
- Ranger chaque **rayon** du ticket dans une **famille** : une table éditable, pré-remplie.
- Ajouter un **onglet Courses** : répartition par famille sur la période, comparaison avec la période
  de comparaison, détail famille → rayons → produits, liste des tickets et de leur rapprochement.
- Préparer Intermarché : ses tickets sont récupérés et conservés dès maintenant, puis relus
  automatiquement quand son lecteur existera.

**Non-objectifs**
- **Ne pas découper le débit bancaire** entre plusieurs catégories (choix 1 de l'utilisateur). Un débit
  rapproché reste entièrement dans sa catégorie (Courses). Le détail par rayon vit dans l'onglet
  Courses et ne change aucun total existant. Le découpage pourra venir plus tard, en s'appuyant sur la
  table rayon → famille.
- Pas de lecteur Intermarché tant qu'aucun vrai ticket n'existe.
- Pas de classement de produits sans rayon (utile pour Intermarché si ses tickets n'ont pas de rayons) :
  ce sera à faire avec le lecteur Intermarché, voir § 8.
- Pas d'envoi de mail, pas de modification de la boîte (aucun marquage lu/non lu, aucun déplacement),
  pas d'autre libellé lu.
- Pas d'IA externe ; aucun ticket ni produit ne quitte le Pi.

**Anti-objectifs (échec même si « ça marche »)**
- **Un rapprochement faux en silence.** S'il y a plusieurs candidats, ou aucun, le ticket attend une
  décision. Il n'est jamais relié au hasard.
- **Un rayon deviné sans le dire.** Un rayon jamais vu va dans « À ranger », pas dans une famille par
  défaut.
- **Un ticket mal lu intégré quand même.** Si la somme des articles lus ne correspond pas au total du
  ticket (aux remises près, voir § 5.3), le ticket est marqué en erreur et ses articles ne comptent pas.
- **Un accès mail plus large que nécessaire**, ou des identifiants Gmail dans le dépôt, l'image ou les
  journaux.
- **Une panne de Gmail (réseau, mot de passe révoqué) qui casse l'application.** La relève échoue
  proprement, l'erreur est visible dans l'onglet Courses, et le reste de l'appli fonctionne.

## 4. Contraintes

- Celles de l'application restent valables : Python pur, pas de dépendance lourde, image Alpine arm64,
  256 Mo de mémoire, système de fichiers en lecture seule sauf `/data` et `/tmp`, tests sur données
  **fictives uniquement**.
- **Aucune nouvelle dépendance** : la relève utilise `imaplib` et `email` (bibliothèque standard), et
  la lecture des PDF utilise `pdfplumber` (déjà présent).
- **Première connexion sortante de l'application** : uniquement vers `imap.gmail.com:993`, en TLS
  avec vérification du certificat.
- **Secrets** : l'adresse Gmail et un **mot de passe d'application** Google (qui nécessite la validation
  en deux étapes sur ce compte) sont fournis par variable d'environnement ou secret Docker, comme le
  mot de passe actuel. Ils sont **facultatifs** : sans eux, la relève est désactivée et seul le dépôt
  manuel fonctionne.
- Données personnelles : l'adresse du compte et les adresses des magasins ne figurent ni dans le dépôt
  ni dans les journaux.

## 5. Décisions de conception

### 5.1 Approches envisagées pour la relève

| Approche | Pour | Contre |
|---|---|---|
| **IMAP + mot de passe d'application, bibliothèque standard** (retenu) | aucune dépendance, un secret de plus comme le mot de passe actuel, Gmail expose chaque libellé comme un dossier IMAP | le mot de passe d'application donne accès à toute la boîte : la limitation aux deux libellés est faite par l'application, pas par Google |
| API Gmail (OAuth) | portée de lecture seule garantie par Google | bibliothèques Google lourdes, jeton à renouveler, consentement OAuth à mettre en place : disproportionné pour un Pi personnel |
| Dépôt manuel seulement | aucune connexion sortante | fastidieux, l'utilisateur veut que ce soit automatique |

**Choix de l'utilisateur :** relève automatique **et** dépôt manuel. Le moyen de relève (IMAP) est un
choix du concepteur.

### 5.2 Relève des tickets
- Une tâche de fond dans le processus de l'application (un thread, comme le reste est déjà sur un seul
  worker) relève les libellés configurés **toutes les heures**, et au démarrage. Un bouton « Relever
  maintenant » existe dans l'onglet Courses. *(choix du concepteur)*
- Libellés relevés, configurables, par défaut : `Super U/Tickets de caisses` → enseigne `superu`,
  `intermarche/Tickets de caisses` → enseigne `intermarche`. Le dossier est ouvert en **lecture seule**
  (`EXAMINE`), donc rien n'est modifié dans la boîte.
- Le premier passage reprend tout l'historique du libellé. Les suivants ne lisent que les messages
  nouveaux : on mémorise l'`UIDVALIDITY` et le dernier `UID` vu par libellé, et un message déjà connu
  (par `Message-ID`) est ignoré.
- Pour chaque message, on garde les pièces jointes PDF (ticket de caisse et ticket CB) et les en-têtes
  utiles (date, expéditeur, objet). Le corps du mail n'est pas conservé.
- Ces fichiers passent ensuite par **le même chemin que le dépôt manuel** (§ 5.3).
- Échec (réseau, authentification, libellé absent) : l'erreur et l'heure de la dernière relève réussie
  sont affichées dans l'onglet Courses. On réessaie au prochain passage, et rien d'autre n'est bloqué.
- Écritures en base : elles passent par le verrou d'écriture existant (`write_lock`) pour ne jamais
  courir en parallèle d'un import de relevé ou d'une ré-analyse.

### 5.3 Import et lecture d'un ticket
- **Entrée commune** : un ou plusieurs PDF, venant de la relève ou d'un dépôt manuel. Un dépôt manuel
  peut contenir un ticket de caisse seul, ou le ticket de caisse et son ticket CB.
- Le fichier déposé passe par les **mêmes contrôles** que les relevés : PDF uniquement, taille maximale,
  dédoublonnage par SHA-256, stockage dans le volume (`/data/receipts/`).
- Le serveur reconnaît s'il s'agit d'un relevé bancaire ou d'un ticket grâce au `can_parse` des
  lecteurs. Le bouton de dépôt existant accepte donc aussi les tickets. *(choix du concepteur ; à
  défaut, une zone de dépôt dans l'onglet Courses)*
- **Lecteurs par enseigne**, sur le modèle de `StatementParser` : interface `ReceiptParser`
  (`name`, `can_parse`, `parse`). Le premier est `superu`.
- **Lecteur Super U** (par coordonnées, comme le relevé) :
  - en-tête : magasin (ville et numéro), date et heure ;
  - après `*** VENTE ***` : un titre de rayon (ligne en gras, sans montant) ouvre un rayon ; chaque
    article est une ligne `libellé … montant €` suivie d'une ligne `n x prix EUR` (quantité) ;
  - les lignes de remise, d'avantage fidélité ou de bon d'achat sont repérées et gardées comme
    lignes négatives du ticket, rattachées à leur rayon quand le ticket le montre, sinon à un rayon
    « Remises » ;
  - le total du ticket et le montant payé par carte sont lus. Le **ticket CB** confirme ce montant
    quand il est présent.
  - Le builder valide ce lecteur sur les vrais tickets en local (sans les commiter), puis écrit les
    tests à partir de tickets **fictifs** générés (comme `tests/pdfgen.py` pour les relevés).
- **Contrôle bloquant** : Σ articles + Σ remises = total du ticket, au centime près. Sinon, le ticket
  est « en erreur » avec l'écart, et ses articles ne comptent pas, comme une section de relevé qui ne
  se réconcilie pas.
- **Enseigne sans lecteur** (Intermarché aujourd'hui) : le ticket est conservé avec le statut
  « en attente de lecteur ». Une **relecture** (bouton dans l'onglet Courses, et automatique au
  démarrage quand un nouveau lecteur apparaît) relit tous les tickets conservés.

### 5.4 Rapprochement ticket ↔ débit *(règles du concepteur, validées dans leur principe)*
- **Montant de référence** : le montant payé par carte (ticket CB, sinon la ligne de paiement carte du
  ticket). Un ticket payé entièrement autrement (espèces, bon d'achat) n'a pas de débit à trouver : il
  est « sans débit carte » et reste visible.
- **Candidats** : les débits du compte courant de **même montant exact**, dont la date d'opération est
  comprise entre le jour du ticket et 3 jours après, et qui ne sont pas déjà reliés à un autre ticket.
- **Un seul candidat** : relié automatiquement, marqué « auto ».
- **Plusieurs candidats** : on garde celui dont le libellé contient un mot du magasin (« U », la ville),
  à condition qu'il soit le seul dans ce cas. Sinon, le ticket attend une décision.
- **Aucun candidat** (relevé pas encore importé, par exemple) : le ticket attend. Le rapprochement est
  retenté à chaque import de relevé et à chaque relève.
- **Lien manuel** : depuis le ticket, l'utilisateur choisit parmi les débits proposés (même montant
  à ±7 jours, puis tout débit de la période), ou délie. Un lien manuel n'est jamais défait
  automatiquement.
- **Identité stable** : le lien est enregistré sur le `tx_key` de la transaction (comme les
  reclassements manuels), pour survivre à une ré-analyse des relevés.
- Le rapprochement **ne change pas la catégorie** du débit.

### 5.5 Rayons et familles
- Le **rayon** est le titre du ticket, normalisé (majuscules, espaces), et rattaché à l'enseigne, car
  deux enseignes n'ont pas les mêmes rayons.
- Une **famille** regroupe des rayons. Liste initiale *(proposition du concepteur, modifiable)* :
  Crèmerie, Fruits & légumes, Viandes & poissons, Épicerie salée, Épicerie sucrée, Boissons, Surgelés,
  Boulangerie & pâtisserie, Hygiène & beauté, Entretien, Maison & divers, Animaux, Remises.
- Une table **rayon → famille** est pré-remplie à partir des rayons trouvés sur les vrais tickets
  Super U. Le fichier de seed ne contient que des noms de rayons génériques, aucune donnée d'achat.
- **Rayon inconnu** : famille vide, visible dans « À ranger » (une section de l'onglet Courses).
  L'utilisateur choisit la famille une fois pour toutes, et tous les articles de ce rayon suivent.
- Les familles et la table sont éditables dans l'onglet Courses.

### 5.6 Onglet Courses
Nouvel onglet `courses`, ajouté au registre, qui suit la période et la période de comparaison de la
barre commune.

```
Courses : 1 sept → 31 oct          312,40 €   (vs période préc. +8 %)
Tickets reliés : 14/16 débits        ⚠ 2 tickets à relier · ⚠ 1 rayon à ranger

Crèmerie            ████████████   78,20 €  ▲ 12 %
Fruits & légumes    ████████       51,10 €  ▼  4 %
Épicerie sucrée     ██████         40,30 €
Hygiène & beauté    ███            18,90 €
…
Non détaillé (débits Courses sans ticket)   24,60 €
```

- **Total** : somme des débits de la catégorie Courses (et sous-catégories) sur la période, nette des
  remboursements, comme l'onglet Catégories. Ventilation par famille : articles des tickets **reliés**
  dont le débit est dans la période. « Non détaillé » = le reste.
- **Tickets non reliés** : listés à part. Leurs articles ne comptent pas dans la répartition, pour que
  la somme des familles reste égale aux débits réels.
- **Clic sur une famille** : ses rayons, puis pour un rayon ses produits (libellé, quantité totale,
  prix moyen, nombre d'achats sur la période), avec la variation par rapport à la période de
  comparaison.
- **Liste des tickets** : date, enseigne, magasin, montant, statut (relié auto, relié à la main, à
  relier, sans débit carte, en erreur, en attente de lecteur). Ouvrir un ticket montre ses articles
  par rayon et le PDF d'origine.
- **Sections d'administration** (repliées) : rayons à ranger et table rayon → famille, état de la
  relève (dernière relève réussie, erreur éventuelle, bouton « Relever maintenant »), bouton
  « Relire les tickets ».
- **Onglet Transactions et détail de catégorie** : un débit relié affiche une icône 🧾 qui ouvre le
  ticket.

### 5.7 Données (nouvelles tables, migration 4 → 5)
Proposition du concepteur, à affiner par le builder :
- `receipts` : id, enseigne, sha256 et chemin du PDF ticket, sha256 et chemin du PDF CB (facultatif),
  origine (imap|upload), Message-ID (facultatif), magasin, date et heure, total, montant carte, statut,
  message d'erreur, `tx_key` relié, mode du lien (auto|manual).
- `receipt_lines` : ticket, rang, rayon, libellé, quantité, prix unitaire, montant (négatif pour une
  remise).
- `aisles` (rayons) : enseigne, nom normalisé, famille (nullable). `aisle_families` : nom, ordre.
- `mail_sync` : libellé, UIDVALIDITY, dernier UID, dernière relève réussie, dernière erreur.

### 5.8 Configuration et déploiement
- Nouvelles variables, toutes facultatives :
  - `EXTRANALYZER_GMAIL_USER` ;
  - `EXTRANALYZER_GMAIL_APP_PASSWORD_FILE` (secret Docker) ou `EXTRANALYZER_GMAIL_APP_PASSWORD` ;
  - `EXTRANALYZER_RECEIPT_LABELS` (format `libellé=enseigne;…`, avec les deux libellés par défaut) ;
  - `EXTRANALYZER_MAIL_INTERVAL_MIN` (60 par défaut).
- `docker-compose.yml` : un second secret facultatif pour le mot de passe d'application.
- README : création du mot de passe d'application Google, secret à créer (propriétaire uid 10001,
  comme le mot de passe actuel), et ce que voit l'onglet quand la relève est désactivée.
- La mémoire reste sous 256 Mo : les PDF sont lus un par un, jamais tous en mémoire.

## 6. Ordre de livraison suggéré
1. Modèle de données et lecteur Super U (validé en local sur les vrais tickets, testé sur des tickets
   fictifs), puis dépôt manuel.
2. Rapprochement avec les débits.
3. Rayons, familles et onglet Courses.
4. Relève IMAP et préparation Intermarché.
5. Configuration, compose, README, puis déploiement sur le Pi.

## 7. Laissé au builder
- Le découpage exact en modules (par exemple `app/receipts/` avec `parsers/`, `matching.py`,
  `mailsync.py`), les noms de routes et le détail du schéma.
- Le repérage précis des lignes de remise et de paiement sur les tickets Super U, d'après les vrais
  tickets.
- La table rayon → famille initiale, d'après les rayons réellement rencontrés.

## 8. Suite prévue (hors périmètre)
- **Lecteur Intermarché**, dès qu'un premier ticket est reçu. Si ses tickets n'ont pas de rayons, on
  rangera chaque produit avec le moteur local de plus proches voisins déjà utilisé pour les
  opérations, appris sur les produits Super U déjà rangés.
- Découpage optionnel du débit par famille dans les totaux de l'application (choix 2, écarté pour
  l'instant).
