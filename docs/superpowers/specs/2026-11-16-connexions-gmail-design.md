# Onglet Connexions : connexion Gmail depuis l'interface (design court)

Date : 2026-11-16 · Statut : **validé par l'utilisateur (2026-11-16)**

## Intention
La relève des tickets de caisse (spec 2026-11-15) ne se configure aujourd'hui que par un secret Docker et
une variable d'environnement. Comme pour la clé Helius, un utilisateur novice doit pouvoir connecter sa
boîte Gmail depuis l'interface web. L'onglet « Banque », qui regroupe déjà les connexions externes
(banques, portefeuilles), devient « Connexions ».

## Décisions
- **Renommage** : titre de l'onglet « Banque » → « Connexions ». L'id `banque` (URL, API, modules) ne change pas.
- **Section « Tickets de caisse (Gmail) »** dans Connexions, sur le modèle de la clé Helius
  (`app/sources/solana/runtime.py`) :
  - formulaire adresse Gmail + mot de passe d'application (espaces retirés), avec une aide courte ;
  - à l'enregistrement : test de connexion IMAP (login + select en lecture seule d'INBOX, aucun message lu,
    aucun drapeau modifié) ; refus → message clair, rien n'est enregistré ;
  - accepté : identifiants écrits atomiquement dans le volume (`/data`, fichier en 0600, via un `.tmp` sans
    trace en cas d'échec), jamais renvoyés par l'API (seule l'adresse, partiellement masquée, est affichée) ;
  - état affiché : connecté / non configuré / géré par le serveur, dernière relève réussie, dernière erreur
    par libellé ; boutons « Relever maintenant » et « Déconnecter » (supprime le fichier, arrête la relève) ;
  - la relève démarre ou s'arrête **sans redémarrage** du conteneur.
- **Priorité serveur** : si `EXTRANALYZER_GMAIL_USER` + mot de passe (variable ou fichier) sont fournis,
  ils priment ; la section affiche « géré par la configuration du serveur » et le formulaire est masqué.
- **Docker** : le secret `gmail_app_password` n'est plus déclaré dans `docker-compose.yml` (plus aucun
  fichier à créer pour mettre à jour). Les variables d'environnement restent possibles. README mis à jour
  (activation par l'onglet Connexions ; route variables d'environnement en option avancée).
- **Libellés** : inchangés (défauts Super U / Intermarché, `EXTRANALYZER_RECEIPT_LABELS`).

## Approches
| Approche | Pour | Contre |
|---|---|---|
| **Fichier d'identifiants dans `/data`, comme la clé Helius** (retenu) | même modèle que l'existant, aucune nouvelle dépendance, pas de redémarrage | le mot de passe d'application est dans les sauvegardes du volume |
| Stockage en base SQLite | une seule source de données | secret mêlé aux données métier, exposé à toute requête SQL |
| Garder le secret Docker | rien à coder | inutilisable par un novice (refusé par l'utilisateur) |

## Anti-objectifs
- Le mot de passe renvoyé par l'API, écrit dans un journal, ou laissé dans un fichier temporaire.
- Un mot de passe invalide enregistré, ou une erreur Gmail qui empêche l'application de démarrer.
- Deux relèves simultanées (ancienne et nouvelle configuration).

## Laissé au builder
Nom du fichier, découpage (runtime Gmail à côté de `app/receipts/mailsync.py`), routes exactes sous
`/api/tabs/banque/`, rendu de la section dans `app/web/tabs/banque.js` ou un module voisin comme
`banque-wallets.js`.
