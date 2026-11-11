# Extranalyzer — classement automatique avec score de confiance (design)

Date : 2026-11-11 · Statut : **validé par l'utilisateur** (2026-11-11)

## 1. Intention

L'utilisateur ne veut plus classer à la main chacune de ses opérations. Le système doit proposer
automatiquement une catégorie avec un **score de confiance** :

- au-dessus du seuil, la catégorie est appliquée d'office ;
- en dessous, l'utilisateur tranche, avec la suggestion déjà remplie.

Deux défauts constatés dans la vue actuelle sont corrigés dans le même lot :

- **Virements livret → compte courant affichés comme des rentrées.** 44 virements à son nom, pour
  10 537 €, sont bien reconnus comme internes et exclus des Revenus. Ils restent pourtant listés dans
  « À classer » avec un « + ».
- **Revenus gonflés.** Les crédits non classés sont comptés en Revenus : 25 623 € sur la période, dont
  21 452 € de virements, en partie depuis des comptes que l'application ne connaît pas.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Classement automatique **local**, qui apprend des classements de l'utilisateur.
- **Confiance ≥ 90 %** : catégorie appliquée automatiquement, marquée « auto » (choix de l'utilisateur).
- **Confiance < 90 %** : l'opération reste dans « À classer » avec la **suggestion pré-remplie** (choix
  de l'utilisateur).
- **Ordre de priorité** : décision manuelle > règle > appariement de virement interne > modèle.
- Chaque correction de l'utilisateur enrichit le modèle immédiatement.
- Un virement interne apparié est classé automatiquement dans « Épargne / <livret concerné> » et
  disparaît de « À classer ».
- Un crédit non classé n'est plus compté comme revenu.

**Non-objectifs**
- Aucune IA externe et aucun envoi de libellés hors du Pi.
- Pas de modèle pré-entraîné « générique » livré avec l'application : on démarre de zéro.
- Pas de nouvelle dépendance lourde (numpy, scipy, scikit-learn, modèle de langage).

**Anti-objectifs**
- Un classement automatique **non signalé** : toute opération classée par le modèle porte une marque
  visible, avec son score.
- Le modèle qui écrase une décision manuelle ou une règle.
- Le modèle qui s'auto-renforce en apprenant de ses propres classements.
- Une image Docker sensiblement plus lourde, ou une mémoire dépassant 256 Mo.

## 3. Contraintes

- Python pur, sans dépendance ajoutée. Volume : environ 1 100 opérations aujourd'hui, quelques
  dizaines de plus par mois.
- Réentraînement et prédiction en moins d'une seconde sur un Raspberry Pi.
- Confidentialité inchangée : les données restent sur le Pi, et les tests utilisent des données
  fictives.
- **Pré-requis :** la branche de suivi `claude/modest-kirch-171d95` (3 correctifs) modifie les mêmes
  fichiers d'analyse et l'onglet Catégories. Elle doit être fusionnée dans `main` avant de commencer.

## 4. Approches envisagées

| Approche | Pour | Contre |
|---|---|---|
| **1. Plus proches voisins sur trigrammes de caractères, en Python pur** (retenu) | aucune dépendance, image inchangée, réentraînement instantané, score explicable (« 4 opérations similaires classées en Courses ») | probabilités moins bien calibrées qu'un modèle statistique |
| 2. scikit-learn (TF-IDF + régression logistique) | probabilités mieux calibrées | environ +100 Mo d'image (numpy/scipy), gain faible pour ce volume |
| 3. Petit modèle de langage local | comprend les libellés sans exemples | centaines de Mo, mémoire, lenteur sur le Pi |

**Choix de l'utilisateur :** approche 1.

## 5. Conception

### 5.1 Ce que mémorise une opération (schéma v3)
Ajouts sur `transactions` :
- `classified_by` : `manual` | `rule` | `pair` | `model` | NULL ;
- `confidence` : REAL ;
- `suggested_category_id` : INT, NULL ;
- `suggestion_reason` : TEXT court (ex. « 4 opérations similaires »).

La migration v2 → v3 suit la logique existante de `connect`. Les lignes existantes reçoivent
`classified_by` d'après leurs colonnes actuelles : `manual=1` → `manual`, `rule_id` non NULL → `rule`,
copie de virement appariée → `pair`.

### 5.2 Chaîne de classement (`on_new_transactions` et chaque correction)
1. **Règles** : comportement actuel. Les lignes `manual`, `pair` et les copies de virement ne sont pas
   touchées.
2. **Appariement des virements internes** : la jambe source non classée et non manuelle reçoit
   « Épargne / <sous-catégorie correspondant au compte d'épargne> », avec `classified_by='pair'`.
   - La correspondance est faite par nom, sans tenir compte de la casse ni des accents :
     « LIVRET BLEU » → Livret Bleu, « LIVRET JEUNE » → Livret Jeune, « COMPTE EPARGNE LOGEMENT » → CEL.
   - Si aucune sous-catégorie ne correspond, une sous-catégorie d'Épargne portant le libellé du compte
     est créée.
   - La copie suit comme aujourd'hui.
3. **Modèle** : pour chaque opération sans catégorie, ou classée `model`, qui n'est ni manuelle, ni
   classée par une règle, ni une copie de virement :
   - le modèle calcule la meilleure catégorie et sa confiance ;
   - si la confiance est ≥ seuil **et** si au moins 3 voisins d'accord sont suffisamment proches :
     `category_id` = la suggestion, `classified_by='model'` ;
   - sinon : `category_id` = NULL, `suggested_category_id` = la suggestion (peut être NULL),
     `classified_by` = NULL.
   - Les classements `model` antérieurs sont recalculés à chaque passage : ils peuvent repasser en
     suggestion si la confiance baisse.

### 5.3 Le modèle (`app/classify/model.py`)
- **Exemples d'apprentissage** : opérations classées `manual`, `rule` ou `pair`, copies de virement
  exclues. **Jamais** les opérations classées par le modèle.
- **Représentation** : trigrammes de caractères du `label_norm` (avec bornes de mots), pondérés TF-IDF,
  puis similarité cosinus.
- **Contraintes dures** : le voisin doit avoir le même sens (débit ou crédit).
- **Bonus léger** : même compte, montant du même ordre de grandeur (tranche logarithmique).
- **Vote** : les k = 7 voisins les plus proches, avec une similarité ≥ 0,35, votent selon leur
  similarité.
- **Confiance** = part du vote obtenue par la catégorie gagnante. Classement automatique seulement si
  la confiance est ≥ 0,90, que la catégorie gagnante réunit au moins 3 voisins et que le meilleur
  voisin a une similarité ≥ 0,6.
- `suggestion_reason` = « N opérations similaires classées en <catégorie> ».
- Le modèle est reconstruit en mémoire à chaque passage. C'est négligeable à ce volume ; si besoin, un
  cache est invalidé à chaque écriture.
- **Seuil** : `EXTRANALYZER_AUTO_THRESHOLD`, défaut `0.90`.

### 5.4 Totaux (modifie la règle § 5.4 bis du design initial)
- **Crédits non classés** : ils ne sont plus ajoutés aux Revenus. Ils sont affichés à part, en
  « Crédits à classer : X € », à côté de la tuile Revenus.
- **Débits non classés** : ils restent comptés dans les Dépenses, comme aujourd'hui. *(choix du
  concepteur : un débit non classé est presque toujours une dépense ; un crédit non classé est souvent
  un virement.)*
- **Solde de la période** = Revenus − Dépenses − Épargne nette, toujours. Les crédits à classer n'y
  entrent pas tant qu'ils ne sont pas classés.

### 5.5 Interface
- **Transactions et Catégories** : un badge « auto 94 % » sur les opérations classées par le modèle.
  Une info-bulle reprend `suggestion_reason`. Le reclassement manuel fonctionne comme aujourd'hui et
  passe la ligne en `manual`.
- **À classer** : chaque groupe montre la suggestion pré-remplie dans les listes déroulantes, avec son
  score. Un bouton « Valider les suggestions affichées » applique d'un coup les suggestions de la page :
  chacune devient une décision manuelle, après confirmation.
- **Vue d'ensemble** : la tuile Revenus porte la mention « + X € de crédits à classer ».
- **Règles** : une ligne d'information indique le nombre d'opérations classées automatiquement et le
  seuil en vigueur.

### 5.6 Tests
- **Modèle** sur des données fictives :
  - apprentissage depuis des exemples `manual` et `rule` uniquement ;
  - sens respecté ;
  - garde des 3 voisins ;
  - seuil appliqué ;
  - classement `model` qui repasse en suggestion quand un exemple contraire est ajouté ;
  - aucun apprentissage depuis des lignes `model`.
- **Appariement** :
  - catégorie Épargne/<livret> appliquée ;
  - création d'une sous-catégorie quand aucune ne correspond ;
  - plus de ligne appariée dans « À classer ».
- **Totaux** :
  - crédits non classés exclus des Revenus et exposés à part ;
  - l'exemple de référence 70 / 1 500 / 205 / 1 225 reste vrai.
- **Migration v2 → v3.**
- **Vérification réelle** (compteurs seulement) : nombre de classements `pair`, `model` et suggestions
  sur les relevés réels, et durée d'un passage complet.

### 5.7 Précisions (après relecture ; *choix du concepteur*)
1. **État dérivé.** `classified_by` (sauf `manual`), `confidence`, `suggested_category_id` et
   `suggestion_reason` sont un état **recalculé** :
   - seules les décisions manuelles (`manual_overrides`) survivent à une ré-analyse ;
   - à chaque passage de la chaîne, le modèle et l'appariement sont recalculés sur **toutes** les lignes
     éligibles, pas seulement les nouvelles, car un nouvel exemple peut changer d'anciennes suggestions.
2. **Colonnes existantes conservées.** `manual` et `rule_id` restent ; `classified_by` s'ajoute à côté
   et doit rester cohérent avec elles.
   - Les copies de virement (`mirror_of` non NULL) ont `classified_by='pair'`.
   - La jambe source reçoit `pair` seulement quand c'est l'appariement qui la classe.
   - Migration : les copies passent en `pair` ; les sources gardent `rule`, `manual` ou NULL.
3. **Priorités.**
   - Une catégorie posée par une règle bloque le classement par appariement : règle > appariement.
   - Effacer une décision manuelle relance toute la chaîne (règles → appariement → modèle) pour cette
     opération.
4. **Correspondance compte → sous-catégorie.**
   - Elle part du libellé du compte (`accounts.label`), sans tenir compte de la casse ni des accents.
   - Un alias intégré couvre « EPARGNE LOGEMENT » → « CEL ». Sinon, on cherche le nom de la
     sous-catégorie contenu dans le libellé.
   - La racine « Épargne » (kind `savings`) existe dans la configuration initiale ; on la crée si elle
     manque. Une sous-catégorie créée prend le kind `savings`.
5. **Données d'apprentissage.**
   - Les sources classées par appariement servent d'exemples.
   - Une sortie du modèle ne devient exemple que si l'utilisateur la valide, ce qui la passe en
     `manual`.
6. **Revenus.**
   - La ligne « À classer » disparaît de la répartition des revenus, pour que les graphiques
     correspondent à la tuile.
   - Les crédits à classer sont exposés à part (montant dans `Totals`).
   - La série mensuelle suit la même règle.
7. **« Valider les suggestions affichées ».**
   - Elle s'applique aux groupes affichés qui ont une suggestion non NULL, quel que soit le score.
   - Elle crée une décision manuelle par opération, sans créer de règle, après une confirmation qui
     indique le nombre d'opérations concernées.

## 6. Laissé au réalisateur
- Les valeurs exactes de k, des similarités minimales et des tranches de montant : partir de celles
  ci-dessus et les ajuster sur des données fictives.
- Le libellé et le style du badge.
- La structure interne du cache, si un cache est nécessaire.
