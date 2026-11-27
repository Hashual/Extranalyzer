# Onglet Budget : objectifs mensuels, suivi, assistant et conseils

## Intention

Fixer un montant mensuel par catégorie de dépense et voir à tout moment où on en est : dépensé, objectif,
pourcentage atteint, ce qui était attendu à cette date et la projection de fin de mois. Être aidé pour choisir
les montants (à partir de ses vraies dépenses et de quelques questions), les modifier facilement, et repérer ce qui
coûte cher, avec des conseils tirés de ses propres chiffres.

## Objectifs / non-objectifs

- Objectifs : par **catégorie racine de dépense** ; **historisés** par mois (un changement s'applique à partir d'un
  mois donné, les mois passés gardent leur objectif d'alors).
- Suivi sur la période de la barre commune (mois, trimestre, année…) et les comptes choisis, comme les autres onglets.
- Non-objectifs : objectifs par sous-catégorie, report du non-dépensé, conseils générés par une IA externe (les
  données ne quittent pas le serveur), alertes ou notifications.
- Anti-objectif : un « attendu à ce jour » linéaire qui crierait au dépassement dès le 2 du mois parce que le loyer
  part le 1er.

## Approches considérées

1. **Objectif unique par catégorie** (sans historique) : le plus simple, mais modifier un montant réécrit le passé.
   Écarté à la demande de l'utilisateur.
2. **Objectifs datés par mois de début d'effet** (retenue) : table `budget_targets(category_id, effective_from,
   monthly_cents)` ; l'objectif d'un mois M est la ligne la plus récente avec `effective_from <= M`. `NULL` = plus
   d'objectif à partir de ce mois.
3. Budgets par période saisie à la main (un budget par mois) : lourd à maintenir, sans gain.

## Calculs (`app/analytics/budget.py`)

- **Habitudes** : les 6 derniers mois complets avant le mois courant (au plus ; ceux qui ont des données).
- **Part habituelle d'une fenêtre de jours** d'un mois (ex. du 1er au 15) : part des dépenses d'une catégorie
  faite d'habitude dans ces jours ; à défaut d'historique, prorata des jours. Un mois entier vaut 1.
- **Objectif de la période** = somme, sur ses mois, de objectif(mois) × part habituelle de la fenêtre couverte.
- **Attendu à ce jour** (période en cours) : même calcul sur la fenêtre déjà écoulée.
- **Projection** : dépensé + dépense habituelle des jours restants de la période (moyenne des mois d'historique) ;
  absente sans historique.
- **Statut** : `over` (dépensé > objectif), `risk` (projection > objectif, ou dépensé > attendu × 1,1), `ok`,
  `none` (sans objectif). Ligne « À classer » sans objectif.
- **Reste par jour** = (objectif total − dépensé total) / jours restants.

## Assistant

Questions : revenu mensuel (pré-rempli : moyenne des revenus des mois d'historique), épargne visée (€ ou % du
revenu), effort (léger 5 %, moyen 10 %, fort 20 % de baisse des postes variables). Un poste est **fixe** si ses
montants mensuels varient peu (coefficient de variation ≤ 20 % et présent ≥ 80 % des mois) : il garde sa moyenne.
Si l'ensemble dépasse revenu − épargne, les postes variables baissent encore, jusqu'à la moitié de leur moyenne au
plus ; au-delà, l'assistant le dit. Arrondi aux 5 € supérieurs. La proposition remplit le formulaire de
modification : rien n'est enregistré sans validation.

## Ce qui coûte cher et conseils

Sur la période : plus gros commerçants (libellé normalisé), petits achats (< 15 €), abonnements (dépenses
récurrentes des 12 derniers mois, coût annuel). Conseils à règles, chacun justifié par un chiffre : objectif dépassé
3 mois de suite, objectif trop large 3 mois de suite, frais bancaires, abonnements, petits achats, catégorie en
hausse de plus de 20 % sur sa moyenne, taux d'épargne vs 20 %, opérations à classer, plus une astuce par catégorie
connue (Courses, Restauration, Transport, Shopping, Loisirs, Factures) quand elle pèse lourd.

## API (`/api/tabs/budget/…`)

`GET /summary` (périmètre), `GET /targets?month=`, `PUT /targets` (`{from_month, targets:[{category_id, amount}]}` ;
remplace aussi les changements postérieurs de ces catégories), `GET /suggest`, `GET /insights` (périmètre).

## Laissé au constructeur

Mise en page de l'onglet, textes des conseils, ordre de l'onglet (entre Vue d'ensemble et Catégories).
Supprimer une catégorie supprime ses objectifs (cascade).
