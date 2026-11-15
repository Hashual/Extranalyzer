# Onglet Épargne en tableau de bord — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** l'onglet Épargne montre ce que l'utilisateur possède : solde des livrets et valeur de la crypto. Il présente
4 chiffres, une répartition en anneau, une évolution en courbe empilée et une carte par endroit. Les mouvements
actuels passent dans une section repliable.

**Architecture :**
- Les calculs vivent dans un module pur `app/analytics/holdings.py` : solde d'un livret à une date, valeur crypto à
  une date, série journalière, répartition, variation à périmètre constant.
- La nouvelle route est `GET /api/tabs/epargne/dashboard`.
- Le client `epargne.js` est réécrit selon la disposition A. La section Banque perd son graphique par portefeuille.

**Tech Stack :** Python 3.12, FastAPI, SQLite, JS vanilla (modules ES), Chart.js embarqué (`app/web/charts.js`).

**Spec :** `docs/superpowers/specs/2026-11-15-epargne-tableau-de-bord-design.md` (§ 5.9 = précisions qui font
autorité).

## Global Constraints

- Une valeur inconnue vaut `None` en Python et `null` en JSON, **jamais 0**.
- Montants internes en centimes (`int`), exposés en `Decimal` d'euros via `from_cents` / `to_jsonable`.
- Le tableau de bord porte sur **tous** les comptes `accounts.kind = 'savings'` et ignore le sélecteur de comptes. La
  route ne prend que `start` et `end`.
- `as_of = min(end, aujourd'hui)`.
  - Valeur au début = fin du jour qui précède `start`.
  - Valeur à la fin = fin du jour `as_of`.
  - Dates de relevés crypto comparées en UTC : `taken_at <= as_of + "T23:59:59.999999+00:00"`.
- **Variation compte par compte** : seuls entrent dans la variation les endroits qui ont une valeur au début ET à la
  fin. `change_scope` vaut `"all"` si tous les endroits connus à la fin y entrent, `"partial"` sinon, et `null` si
  aucun n'y entre.
- Une requête GET n'écrit rien en base.
- Les avoirs sont affichés avec `money(v, "balance")`, les flux avec `money(v, "savings")`. Les axes et les
  info-bulles des graphiques suivent le masquage.
- Pas de nouvelle dépendance. Pas d'`innerHTML` : libellés via `el(..., {text})`.
- Tests sur des données fictives uniquement. Ne jamais ouvrir `extraits/`, `*.xlsx` ni les secrets. Ne jamais pousser.
- Commits en français, avec une ligne `Co-Authored-By` qui nomme le modèle.
- Tests : `C:/Users/<utilisateur>/Documents/Perso/Applications/Extranalyzer/.venv/Scripts/python -m pytest`.

## Review Focus

1. **Aucune donnée** (ni livret ni portefeuille) : la réponse est valide, avec `places: []`, des séries `null` et un
   écran d'accueil côté client. Testé en tâche 2 (route) et vérifié en tâche 3.
2. **Période entièrement passée, avant tout suivi crypto** : `crypto.value = null`, la crypto est absente de la
   répartition, et aucune erreur. Testé en tâche 1.
3. **Période qui finit dans le futur** : la série s'arrête à aujourd'hui et `as_of = aujourd'hui`. Testé en tâche 1.
4. **Portefeuille ajouté mais jamais lu** : il est compté dans `crypto.unread_count`, sa carte affiche « valeur
   inconnue », et il n'est pas compté pour 0. Testé en tâche 1.
5. **Mode discret activé** : les tuiles, les cartes, les axes, les info-bulles et la légende de l'anneau sont masqués.
   Vérifié en tâche 3.

---

### Task 1 : Calculs des avoirs (`app/analytics/holdings.py`)

**Files :**
- Create : `app/analytics/holdings.py`
- Modify : `app/classify/savings_target.py` (paramètre `create`)
- Test : `tests/test_holdings.py`

**Interfaces :**
- Produces :

```python
# app/classify/savings_target.py
def savings_category_for_account(conn, account_id: int, create: bool = True) -> int | None
#   create=False : même recherche, renvoie None au lieu d'insérer (aucune écriture)

# app/analytics/holdings.py
MAX_DAILY_POINTS = 400
ASSET_SLICES = 6            # au-delà : les plus petites parts regroupées en « Autres »
def savings_balance(conn, account_id: int, day: date) -> int | None
def wallet_value(conn, wallet_id: int, day: date) -> int | None
def crypto_tracked_since(conn) -> date | None
def series(conn, start: date, end: date) -> list[dict]          # [{date, savings: int|None, crypto: int|None}]
def dashboard(conn, period: Period, today: date) -> dict          # forme ci-dessous, montants en centimes
```

Règles (spec § 5.1 à 5.4 et § 5.9) :
- **`savings_balance`** :
  - prendre `closing_cents` et `statement_date` du dernier relevé `ok` du compte avec `statement_date <= day` ;
  - y ajouter `SUM(amount_cents)` des opérations du compte avec `statement_date < op_date <= day`, jambes miroir
    comprises ;
  - sans relevé antérieur, renvoyer `None`.
- **`wallet_value`** : `total_eur_cents` du dernier relevé `ok` avec `taken_at <= fin du jour`, sinon `None`.
- **`series`** :
  - un point par jour de `start` à `end` ;
  - si cela fait plus de `MAX_DAILY_POINTS` jours, un point par semaine, au dernier jour de chaque semaine ;
  - `savings` = somme des soldes connus, ou `None` si aucun n'est connu ;
  - `crypto` = somme des valeurs connues, ou `None` avant `crypto_tracked_since` ou si aucune n'est connue.
  - Pour rester rapide, charger relevés et opérations en mémoire une fois par compte, puis avancer jour par jour. Pas
    de requête par jour.
- **`dashboard(conn, period, today)`**, avec `as_of = min(period.end, today)` et `before = period.start - 1 jour` :

```
{as_of, total: {value, change, change_scope},
 savings: {value, count, unknown_count},
 crypto: {value, count, unread_count, unpriced_count, deposited, gap, tracked_since},
 places: [{kind, id, name, category_id, value, change, deposited, gap, unknown}],
 allocation: {by_place: [{name, value}], by_asset: [{name, value}]},
 series: series(conn, period.start, as_of)}
```

- Détail des champs de `dashboard` :
  - `places` : un élément par livret (`kind = 'savings'`, `category_id` via `savings_category_for_account(...,
    create=False)`, `deposited` et `gap` à `None`) et un par portefeuille (`kind = 'wallet'`, `category_id =
    savings_category_id`, `deposited` = net versé comme dans `wallets._net_deposited`, `gap = value - deposited` si
    `value` est connue).
  - `change` = valeur à `as_of` − valeur à `before`, si les deux sont connues ; sinon `None`.
  - Tri des `places` par valeur décroissante, les inconnues à la fin.
  - `crypto.unpriced_count` : somme des `unpriced_count` des relevés retenus.
  - `crypto.deposited` : somme des `deposited` des portefeuilles.
  - `crypto.gap` : `crypto.value - crypto.deposited` si `crypto.value` est connue.
  - `total.value` = livrets + crypto connus, ou `None` si rien n'est connu.
  - `total.change` = somme des `change` non nuls, ou `None` si aucun ; `change_scope` suit les Global Constraints.
  - `by_place` : les `places` dont la valeur est connue et positive.
  - `by_asset` :
    - une part « Livrets » = `savings.value` ;
    - pour chaque portefeuille, on prend les positions du relevé retenu (dernier `ok` ≤ `as_of`), sans les mints de
      `hidden_mints` ni les positions dont `value_eur_cents` est `NULL` ;
    - `stake` donne « SOL en staking », `sol` donne « SOL disponible », un `token` est regroupé par `mint` sous son
      `symbol` (repli sur le mint raccourci) ;
    - tri décroissant ; au-delà de `ASSET_SLICES` parts, les suivantes sont fusionnées en « Autres ».

- [ ] **Step 1 : Tests en échec** (`tests/test_holdings.py`, base neuve via `app.store.db.connect`, données insérées
  en SQL, montants fictifs)
  - `test_savings_balance_from_statement_and_later_ops` :
    - données : un relevé `ok` au 2026-01-31 avec une clôture de 100 000 centimes, puis des opérations de +5 000 au
      02-03 (source `'enablebanking'`) et de −2 000 au 02-10 ;
    - attendu : solde 103 000 au 02-15, 105 000 au 02-05, 100 000 au 01-31, `None` au 01-15.
  - `test_wallet_value_carries_last_ok_snapshot` : les relevés `ok` du 11-10 (1 000) et du 11-12 (1 500) et un relevé
    `error` du 11-13 donnent 1 500 au 11-14, 1 000 au 11-11 et `None` au 11-09.
  - `test_series_daily_and_weekly` :
    - sur une période de 10 jours, il y a 10 points et `crypto is None` avant le premier relevé ;
    - sur une période de 500 jours, les points sont hebdomadaires (≈ 72) et le dernier tombe à la date de fin.
  - `test_dashboard_change_is_per_place` : un livret connu au début et à la fin, plus un portefeuille dont le premier
    relevé tombe en cours de période, donnent `total.change` = variation du livret seule et `change_scope ==
    "partial"`.
  - `test_dashboard_unread_wallet_not_zero` : un portefeuille sans relevé a `value is None` et `unknown is True`, et
    `crypto.unread_count == 1`.
  - `test_dashboard_past_period_before_crypto` : une période close avant tout relevé crypto donne `crypto.value is
    None` et aucune part crypto dans `by_asset`.
  - `test_dashboard_future_end` : avec `end` après `today`, `as_of == today` et le dernier point de la série vaut
    `today`.
  - `test_by_asset_groups_and_hides` : 9 jetons valorisés donnent au plus `ASSET_SLICES + 1` parts, dont « Autres ».
    Un mint masqué en est absent, ainsi qu'une position sans valeur. « SOL en staking » et « SOL disponible » sont
    présents.
  - `test_category_lookup_does_not_create` : `savings_category_for_account(conn, id, create=False)` renvoie `None` et
    le nombre de lignes de `categories` ne change pas, pour un compte sans catégorie correspondante.
- [ ] **Step 2 :** `pytest tests/test_holdings.py -v` → échec (module absent).
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** relancer, puis `pytest` complet → PASS.
- [ ] **Step 5 : Commit** `feat: calcul des avoirs d'épargne (livrets, crypto, série, répartition)`

---

### Task 2 : Route `GET /api/tabs/epargne/dashboard`

**Files :**
- Modify : `app/tabs/epargne.py`
- Test : `tests/test_tabs_api.py` (à côté de `test_epargne_summary`)

**Interfaces :**
- Consumes : `holdings.dashboard(conn, period, today)` (tâche 1) ; `Period` de `app/analytics/periods.py`.
- Produces : `GET /api/tabs/epargne/dashboard?start=YYYY-MM-DD&end=YYYY-MM-DD`. Elle renvoie le dict de `dashboard`
  avec les centimes convertis en `Decimal` d'euros (`from_cents`) dans `total`, `savings`, `crypto`, `places`,
  `allocation` et `series`, `None` étant conservé, le tout via `to_jsonable`. Un `start > end` donne 422.
  `today = date.today()`, injectable pour les tests (paramètre de module ou dépendance, au choix).

- [ ] **Step 1 : Tests en échec**
  - `test_epargne_dashboard_shape` :
    - avec la fixture `env` existante, on obtient un statut 200 et les clés `as_of`, `total`, `savings`, `crypto`,
      `places`, `allocation` et `series` ;
    - les montants sont des chaînes décimales ;
    - une valeur inconnue sort en `null`.
  - `test_epargne_dashboard_empty_db` : une base vide donne un statut 200, `places == []`, `total.value is None`, et
    chaque point de la série a `savings` et `crypto` à `null`.
  - `test_epargne_dashboard_ignores_accounts_param` : le résultat est identique avec ou sans `accounts=1`.
  - `test_epargne_dashboard_bad_period_422`.
  - Ajouter `"/epargne/dashboard"` à la liste des routes protégées par l'authentification
    (`tests/test_tabs_api.py:79`).
- [ ] **Step 2 :** lancer les tests → échec (404).
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** `pytest` complet → PASS (`/summary` inchangé).
- [ ] **Step 5 : Commit** `feat: route du tableau de bord d'épargne`

---

### Task 3 : Interface — onglet Épargne en tableau de bord, section Banque allégée

**Files :**
- Modify : `app/web/tabs/epargne.js` (réécriture du rendu, l'ancien contenu passe dans la section repliable)
- Modify : `app/web/tabs/banque-wallets.js`
  - retirer `historyChart` et son appel ;
  - ajouter le lien « Voir l'analyse dans Épargne » (`ctx.navigate("epargne")`), si `hasTab(ctx, "epargne")`.
- Modify : `app/web/style.css` (grilles des tuiles, des deux colonnes et des cartes ; une colonne sous 768 px)

**Interfaces :**
- Consumes :
  - routes `/tabs/epargne/dashboard` (tâche 2) et `/tabs/epargne/summary` (existante, avec `scopeParams(ctx)`) ;
  - helpers `el`, `section`, `money`, `amount`, `emptyNote`, `hasTab`, `scopeParams`, `formatMonth` de
    `app/web/ui.js` ;
  - `mountChart`, `cssVar` de `app/web/charts.js` ;
  - `isMasked`, `MASK` de `app/web/privacy.js`.

Contenu (spec § 5.6, maquette A) :
1. **Tuiles** :
   - « Épargne totale », avec la variation, suivie de « (livrets seuls, crypto suivie depuis le JJ/MM) » quand
     `change_scope == "partial"` ;
   - « Livrets » (valeur et nombre de comptes, plus « hors N sans solde connu ») ;
   - « Crypto » (valeur et nombre de portefeuilles, plus les mentions sans relevé et sans prix) ;
   - « Versé → valeur », avec l'écart coloré.
2. **Deux colonnes** :
   - Répartition : `doughnut` sur `by_place` ou `by_asset`, avec deux boutons qui basculent sans recharger, et une
     légende qui montre les parts en %. Les % sont masqués en mode discret.
   - Évolution : `line` empilée (`stacked: true`, `fill: true`) sur `series`, avec les couches « Livrets » et
     « Crypto ». Sous la courbe, la note « Crypto suivie depuis le … » si `crypto.tracked_since`.
3. **Cartes « Où »** :
   - nom, valeur ou « valeur inconnue », variation si elle n'est pas `null` ;
   - pour un portefeuille, « Versé X € · écart ±Y € ».
   - Un clic sur un livret avec `category_id` ouvre `ctx.navigate("categories", { id })`. Un clic sur un portefeuille
     ouvre `ctx.navigate("banque")`.
4. **Section repliable** `<details>` « Mouvements de la période » : contenu actuel (tuiles des flux, graphique net
   cumulé, tableau par destination), chargé avec `/summary`.
5. **Aucune donnée** (`places` vide) : message d'accueil qui renvoie vers Banque (« ajouter un portefeuille ») et vers
   le dépôt de relevés.

- [ ] **Step 1 :** implémenter. `node --check` sur les fichiers JS modifiés.
- [ ] **Step 2 : Vérification dans l'appli** :
  - démo de données fictives : lanceur hors dépôt, sur le modèle de
    `C:/Users/<utilisateur>/AppData/Local/Temp/claude/df5a50437fcf1dd4/7ef0df93-ca28-4afa-8b77-9c6dcaecb76d/scratchpad/demo_wallets.py`,
    étendu avec un livret et quelques relevés PDF fictifs insérés en SQL, plus plusieurs jours de relevés crypto ;
  - vérifier sur ordinateur (thème clair) et à 375 px (thème sombre) : bascule de l'anneau, courbe empilée, cartes,
    mode discret (tout masqué, y compris les axes et les info-bulles), base vide (écran d'accueil) ;
  - console sans erreur.
- [ ] **Step 3 :** `pytest` complet → PASS.
- [ ] **Step 4 : Commit** `feat: onglet Épargne en tableau de bord (répartition, évolution, où)`
