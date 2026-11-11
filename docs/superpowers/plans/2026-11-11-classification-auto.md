# Classement automatique avec score de confiance — plan de réalisation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** classer automatiquement les opérations à l'aide d'un modèle local par plus proches voisins.
- Confiance ≥ 90 % → catégorie appliquée, marquée « auto ».
- Confiance < 90 % → suggestion pré-remplie dans « À classer ».
- Les virements appariés sont classés en « Épargne / <livret> ».
- Les crédits non classés ne comptent plus en Revenus.

**Architecture :**
- Une fonction unique `reclassify(conn)` enchaîne règles → appariement → modèle → synchronisation des
  miroirs sur toutes les lignes éligibles. L'import et toutes les écritures de classement l'appellent.
- Le modèle (`app/classify/model.py`) est en Python pur et sans état persistant : il est reconstruit à
  chaque passage à partir des exemples `manual`, `rule` et `pair`.

**Tech Stack :** Python 3.11+ (stdlib uniquement pour le modèle), SQLite, FastAPI, JS vanilla (onglets
existants).

**Spec :** `docs/superpowers/specs/2026-11-11-classification-auto-design.md`

## Global Constraints
- Aucune dépendance ajoutée. Pas de numpy, scipy ni scikit-learn.
- Aucune donnée réelle dans le dépôt, les tests ou les logs. Ne jamais ouvrir `extraits/`, `data/`,
  `secrets/` ni un `*.xlsx`.
- Montants en `Decimal` dans le code, en centimes `INTEGER` en base. Négatif = débit.
- Priorité : `manual` > `rule` > `pair` > `model`.
- Le modèle n'apprend jamais d'une ligne `classified_by='model'`.
- Seuil par défaut 0,90, variable `EXTRANALYZER_AUTO_THRESHOLD`.
- Classement automatique seulement si : confiance ≥ seuil, au moins 3 voisins d'accord, et meilleur
  voisin avec une similarité ≥ 0,6.
- Voisins : k = 7, similarité minimale 0,35, même sens obligatoire (débit/crédit).
- Interface en français, CSP stricte : uniquement `textContent`, aucun style ni script en ligne.
- Commits terminés par une ligne `Co-Authored-By: Claude <modèle> <noreply@anthropic.com>`.

## Review Focus
1. **Base sans aucun exemple classé :** aucun classement automatique, aucune suggestion, aucune
   exception. Test dans la Tâche 3.
2. **Deux catégories à égalité de vote :** confiance de 0,5 au plus, donc suggestion et jamais de
   classement automatique. Test dans la Tâche 3.
3. **Suppression d'une règle qui classait des opérations :** les opérations repassent par appariement
   puis par le modèle, au lieu de rester NULL. Test dans la Tâche 4.
4. **Retour en arrière sur une décision manuelle** (`category_id=None`) **d'une jambe appariée :** la
   ligne redevient `pair` (Épargne/<livret>). Test dans la Tâche 4.
5. **Compte d'épargne au libellé inconnu :** une sous-catégorie d'Épargne est créée une seule fois,
   jamais en double au passage suivant. Test dans la Tâche 2.

---

### Task 1 : schéma v3 et cohérence de `classified_by`

**Files :**
- Modify : `app/store/schema.sql`, `app/store/db.py` (`SCHEMA_VERSION = 3`, migration 2→3),
  `app/classify/rules.py` (écrire `classified_by`)
- Test : `tests/test_store.py`, `tests/test_rules.py`

**Interfaces — Produces :** colonnes ajoutées à `transactions` :
- `classified_by TEXT NULL CHECK(classified_by IN ('manual','rule','pair','model'))` ;
- `confidence REAL NULL` ;
- `suggested_category_id INTEGER NULL REFERENCES categories(id)` ;
- `suggestion_reason TEXT NULL`.

**Migration 2→3** (une seule transaction, même schéma de garde que 1→2) : ajouter les colonnes, puis
remplir `classified_by` :
- `manual=1` → `'manual'` ;
- `rule_id NOT NULL` → `'rule'` ;
- `mirror_of NOT NULL` → `'pair'`.

`apply_rules` écrit `classified_by='rule'` sur une ligne classée et `NULL` sinon.
`set_manual_category` écrit `'manual'` ou `NULL`. `sync_mirrors` écrit `'pair'` sur les miroirs.

- [ ] **Step 1 :** écrire les tests :
  - `test_migration_2_to_3` : une base v2 avec une ligne manuelle, une ligne classée par une règle et un
    miroir donne `('manual','rule','pair')` et `user_version == 3` ; un second `connect` ne fait rien ;
  - `test_apply_rules_sets_classified_by` ;
  - `test_manual_sets_and_clears_classified_by`.
- [ ] **Step 2 :** lancer `.venv/Scripts/python -m pytest tests/test_store.py tests/test_rules.py -q`.
  Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** relancer ces tests puis la suite complète. Attendu : PASS.
- [ ] **Step 5 :** commit `feat: schéma v3 — origine et confiance du classement`

---

### Task 2 : classement des virements appariés en Épargne / <livret>

**Files :**
- Create : `app/classify/savings_target.py`
- Modify : `app/classify/transfers.py`
- Test : `tests/test_transfers.py`

**Interfaces — Produces :**
```python
def savings_category_for_account(conn, account_id: int) -> int
    # sous-catégorie d'Épargne pour ce compte d'épargne : créée si besoin, idempotente
def classify_paired_sources(conn) -> int
    # jambes sources appariées sans catégorie et non manuelles -> catégorie Épargne/<compte du miroir>,
    # classified_by='pair' ; renvoie le nombre classées
```

**Correspondance** : libellé du compte, sans tenir compte de la casse ni des accents.
- L'alias `{"EPARGNE LOGEMENT": "CEL"}` est appliqué en premier.
- Sinon, on retient la sous-catégorie d'Épargne dont le nom normalisé est contenu dans le libellé.
- Sinon, on crée la sous-catégorie avec le libellé du compte en casse « Titre », kind `savings`.
- La racine « Épargne » (kind `savings`) est créée si elle manque.
- Une source qu'une règle a déjà classée garde sa catégorie : règle > appariement.

- [ ] **Step 1 :** écrire les tests :
  - `test_paired_source_gets_livret_category` : comptes « LIVRET BLEU » et courant, sous-catégorie
    seedée « Livret Bleu » → la source a cette catégorie et `classified_by == 'pair'`, et le miroir
    suit après `sync_mirrors` ;
  - `test_cel_alias` ;
  - `test_unknown_savings_account_creates_subcategory_once` : deux appels → une seule sous-catégorie
    **(Review Focus 5)** ;
  - `test_rule_category_blocks_pair_classification` ;
  - `test_manual_source_untouched`.
- [ ] **Step 2 :** lancer `pytest tests/test_transfers.py -q`. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** relancer. Attendu : PASS.
- [ ] **Step 5 :** commit `feat: virements appariés classés en Épargne / livret`

---

### Task 3 : modèle par plus proches voisins (pur, sans base)

**Files :**
- Create : `app/classify/model.py`
- Test : `tests/test_model.py`

**Interfaces — Produces :**
```python
@dataclass(frozen=True)
class Example: label_norm: str; amount_cents: int; account_id: int; category_id: int
@dataclass(frozen=True)
class Prediction:
    category_id: int | None; confidence: float; support: int; best_similarity: float; reason: str
    def is_auto(self, threshold: float) -> bool   # confidence>=threshold and support>=3 and best_similarity>=0.6
def build_index(examples: list[Example]) -> "Index"
def predict(index, label_norm: str, amount_cents: int, account_id: int) -> Prediction
```

**Algorithme (décisions fixées) :**
- **Trigrammes de caractères** de `" " + mot + " "` pour chaque mot du libellé normalisé.
- **Pondération** TF-IDF avec `idf = log((1+N)/(1+df)) + 1`, puis similarité cosinus.
- **Candidats :** même signe que l'opération (débit ou crédit), similarité ≥ 0,35, les 7 meilleurs.
- **Poids d'un voisin** = sa similarité. Bonus multiplicatif de +10 % si même compte, et de +10 % si
  `floor(log10(|montant|))` est identique.
- **Confiance** = poids de la catégorie gagnante ÷ poids total.
- **Support** = nombre de voisins de la catégorie gagnante.
- `reason` = `"{support} opération(s) similaire(s) classée(s) ainsi"`.
- **Sans voisin :** `Prediction(None, 0.0, 0, 0.0, "aucune opération similaire")`.

- [ ] **Step 1 :** écrire les tests (données fictives) :
  - `test_empty_index_predicts_nothing` **(Review Focus 1)** ;
  - `test_identical_labels_auto` : 4 exemples « MAGASIN ALPHA » en Courses → `category_id` Courses,
    `is_auto(0.9)` vrai ;
  - `test_support_guard` : 2 exemples seulement → `is_auto` faux même avec une confiance de 1,0 ;
  - `test_tie_never_auto` : 3 + 3 exemples proches dans deux catégories → confiance ≤ 0,5 et `is_auto`
    faux **(Review Focus 2)** ;
  - `test_direction_hard_filter` : un crédit ne prend jamais une catégorie apprise uniquement sur des
    débits ;
  - `test_reason_text`.
- [ ] **Step 2 :** lancer `pytest tests/test_model.py -q`. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** relancer. Attendu : PASS.
- [ ] **Step 5 :** commit `feat: modèle local de classement par plus proches voisins`

---

### Task 4 : chaîne `reclassify` et branchement de toutes les écritures

**Files :**
- Modify : `app/classify/pipeline.py`, `app/classify/rules.py`, `app/config.py`
- Test : `tests/test_pipeline.py` (create), ajustements dans `tests/test_rules.py` et
  `tests/test_transfers.py`

**Interfaces :**
- **Consumes :** Tasks 1 à 3.
- **Produces :**
```python
def reclassify(conn, threshold: float | None = None) -> dict[str, int]
    # renvoie {"rule": n, "pair": n, "model": n, "suggested": n, "unclassified": n}
def on_new_transactions(conn, new_ids) -> None    # appelle reclassify(conn) ; signature inchangée
# config.Settings gagne auto_threshold: float = 0.90 (env EXTRANALYZER_AUTO_THRESHOLD, borné ]0,1])
```
Quand `threshold` vaut None, `reclassify` lit `EXTRANALYZER_AUTO_THRESHOLD`, et à défaut 0,90.

**Ordre dans `reclassify`, sans commit :**
1. `apply_rules(conn)` sur toutes les lignes ;
2. `pair_internal_transfers(conn)` ;
3. `classify_paired_sources(conn)` ;
4. modèle : les exemples sont les lignes non miroir dont `classified_by` vaut `manual`, `rule` ou `pair`.
   Pour chaque ligne non miroir dont `classified_by` est NULL ou `model` :
   - `is_auto` → `category_id`, `classified_by='model'`, `confidence`, `suggestion_reason`,
     `suggested_category_id = category_id` ;
   - sinon → `category_id = NULL`, `classified_by = NULL`, `suggested_category_id`, `confidence` et
     `suggestion_reason` remplis à partir de la prédiction (`suggested_category_id` peut être NULL) ;
5. `sync_mirrors(conn)`.

À chaque passage, `confidence`, `suggestion_reason` et `suggested_category_id` sont remis à NULL sur les
lignes `manual`, `rule` et `pair`.

**Mutations qui appellent désormais `reclassify(conn)` au lieu d'un `apply_rules` partiel :**
`create_rule`, `update_rule`, `delete_rule`, `set_manual_category` (dans les deux sens). Les sources
miroirs sont toujours redirigées.

`set_manual_category` gagne un argument nommé `reclassify_after: bool = True`. La validation groupée
(Tâche 6) passe `False` puis appelle `reclassify` une seule fois.

- [ ] **Step 1 :** écrire les tests :
  - `test_reclassify_order_and_counts` ;
  - `test_model_never_learns_from_model_rows` : après un premier passage `model`, supprimer les
    exemples manuels et relancer → les lignes `model` repassent en NULL ;
  - `test_model_row_downgraded_when_contradicting_example_added` ;
  - `test_delete_rule_falls_back_to_pair_then_model` **(Review Focus 3)** ;
  - `test_clearing_manual_on_paired_source_returns_to_pair` **(Review Focus 4)** ;
  - `test_threshold_from_env` ;
  - `test_reclassify_does_not_commit`.
- [ ] **Step 2 :** lancer `pytest tests/test_pipeline.py -q`. Attendu : échec.
- [ ] **Step 3 :** implémenter. Adapter les tests existants qui supposaient un `apply_rules` partiel
  uniquement si leur intention reste vraie ; le noter dans le rapport.
- [ ] **Step 4 :** lancer la suite complète. Attendu : PASS.
- [ ] **Step 5 :** commit `feat: chaîne de classement unique (règles, virements, modèle)`

---

### Task 5 : totaux — crédits non classés hors Revenus

**Files :**
- Modify : `app/analytics/totals.py`
- Test : `tests/test_analytics.py`

**Changements :**
- `Totals.income` n'additionne plus `unclassified_credits`, qui reste exposé à part.
- `balance = income − expenses − savings_net`.
- `by_category(kind="income")` ne renvoie plus de ligne « À classer ». La ligne « À classer » des
  dépenses reste.
- `monthly_series` suit, puisqu'elle passe par `totals` et `by_category`.

- [ ] **Step 1 :** écrire les tests :
  - `test_unclassified_credit_not_income` : un crédit de 50 non classé → `income` inchangé,
    `unclassified_credits == 50.00`, `balance` sans les 50 ;
  - `test_income_breakdown_has_no_unclassified_row` ;
  - garder `test_totals_all_accounts` (70 / 1500 / 205 / 1225) vert.
- [ ] **Step 2 :** lancer `pytest tests/test_analytics.py -q`. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer `test_analytics.py` et `test_tabs_api.py`. Attendu : PASS (adapter les
  attentes de `test_tabs_api` qui comptaient les crédits non classés en revenus).
- [ ] **Step 5 :** commit `fix: crédits non classés exclus des revenus`

---

### Task 6 : API et interface (badge auto, suggestions, validation, revenus)

**Files :**
- Modify : `app/tabs/_transactions.py`, `app/tabs/transactions.py`, `app/tabs/categories.py`,
  `app/tabs/a_classer.py`, `app/tabs/overview.py`, `app/tabs/regles.py`, `app/web/tabs/*.js`
  concernés, `app/web/style.css`
- Test : `tests/test_tabs_api.py`

**API :**
- **Lignes de transaction** (liste Transactions et détail Catégories) : champs ajoutés `classified_by`,
  `confidence` (nombre ou null), `suggestion_reason`.
- **`GET /api/tabs/a_classer/list` :** chaque groupe gagne `suggested_category_id`, `suggested_name`,
  `confidence` et `reason`. Ce sont ceux de la ligne du groupe qui a la plus forte confiance, ou null.
- **`POST /api/tabs/a_classer/accept_suggestions {label_norms: [str]}` :**
  - dans la portée de la barre, applique `set_manual_category(tx, suggested_category_id)` à chaque
    opération des groupes listés qui a une suggestion non NULL ;
  - un seul `reclassify` à la fin, puis commit ;
  - renvoie `{classified: n, unclassified_count}` ;
  - liste vide → 422.
- **Overview `summary` :** `totals.unclassified_credits` est déjà présent ; l'interface l'affiche.
- **Règles :** `GET /api/tabs/regles/list` gagne `auto: {count, threshold}`, où `count` = lignes
  `classified_by='model'`.

**Interface :**
- **Badge** `auto NN %` sur les lignes `model`, dans Transactions et Catégories, avec un `title` égal à
  `suggestion_reason`.
- **À classer :**
  - les listes déroulantes sont pré-remplies avec la suggestion et affichent son score ;
  - un bouton « Valider les suggestions affichées (N) » demande `confirm()` en indiquant le nombre
    d'opérations, appelle la route ci-dessus, puis `ctx.refresh()`.
- **Vue d'ensemble :** sous la tuile Revenus, « + X € de crédits à classer » quand X > 0.
- **Règles :** une ligne d'information « N opérations classées automatiquement (seuil 90 %) ».

- [ ] **Step 1 :** écrire les tests :
  - `test_transactions_list_exposes_classified_by_and_confidence` ;
  - `test_a_classer_group_has_suggestion` ;
  - `test_accept_suggestions_creates_manual_decisions` : les opérations deviennent `manual`, le compteur
    baisse et aucune règle n'est créée ;
  - `test_accept_suggestions_empty_422` ;
  - `test_regles_list_auto_info`.
- [ ] **Step 2 :** lancer `pytest tests/test_tabs_api.py -q`. Attendu : échec.
- [ ] **Step 3 :** implémenter (API puis JS).
- [ ] **Step 4 :** lancer la suite complète. Attendu : PASS. Puis vérifier dans le navigateur avec
  `uvicorn --factory app.main:get_app`, un dossier de données temporaire et des PDF synthétiques :
  - badge, pré-remplissage, validation groupée, mention sous Revenus ;
  - thème clair et sombre ;
  - largeur 375 px ;
  - aucune erreur CSP.
- [ ] **Step 5 :** commit `feat: interface du classement automatique`

---

### Task 7 : script de vérification — compteurs du classement

**Files :**
- Modify : `scripts/check_real_statements.py`
- Test : `tests/test_check_real_statements.py`

Le script ajoute ces compteurs : classées par règle, par appariement, automatiquement (modèle),
suggestions sous le seuil, sans suggestion, et durée du dernier `reclassify` en millisecondes (entier).
La sortie reste limitée à des compteurs.

- [ ] **Step 1 :** écrire le test : les nouveaux libellés sont présents et sont des entiers ; aucune donnée
  synthétique n'apparaît dans la sortie.
- [ ] **Step 2 :** lancer le test. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer le test puis la suite complète. Attendu : PASS.
- [ ] **Step 5 :** commit `chore: compteurs de classement dans le script de vérification`
- [ ] **Step 6 (contrôleur) :** lancer le script sur `extraits/` (compteurs seulement) et transmettre les
  chiffres à l'utilisateur.
