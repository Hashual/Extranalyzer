# Extranalyzer — onglet Épargne en tableau de bord (design)

Date : 2026-11-15 · Statut : **validé par l'utilisateur** (2026-11-15)

## 1. Intention

Le suivi des portefeuilles Solana est en place (`2026-11-15-portefeuilles-solana-design.md`), dans une section de
l'onglet Banque. L'utilisateur trouve que cette analyse a sa place dans **Épargne** : la crypto est de l'épargne. Il
reproche aussi à la première version l'absence de graphiques et de vue d'évolution.

Ce qu'il veut comprendre d'un coup d'œil, dans ses mots : **« la répartition de mon compte, les évolutions, et savoir
combien j'ai et où ».**

Aujourd'hui, l'onglet Épargne ne montre que des **flux bancaires** (versements, retraits, net cumulé) et jamais ce que
l'utilisateur **possède**. Le tableau de bord ajoute les avoirs : solde des livrets et valeur de la crypto.

## 2. Décisions de l'utilisateur

- **Disposition A, « tableau de bord »**, choisie sur maquettes :
  1. quatre chiffres en haut ;
  2. la répartition (anneau) et l'évolution (courbe) côte à côte ;
  3. une carte par endroit où l'épargne est placée.
- **Évolution à partir d'aujourd'hui pour la crypto** : on ne reconstitue pas l'historique passé des portefeuilles. Ce
  sera peut-être une étape ultérieure.
- **Courbe empilée livrets + crypto** :
  - les livrets, dont l'historique est reconstituable, couvrent toute la période ;
  - la crypto s'empile par-dessus à partir de son premier relevé.

## 3. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Quatre chiffres (KPI), valeurs à la **fin de la période** sélectionnée :
  - **Épargne totale** = livrets + crypto, avec sa variation sur la période (voir § 5.4) ;
  - **Livrets** : somme des soldes des comptes d'épargne et nombre de comptes ;
  - **Crypto** : valeur des portefeuilles et nombre de portefeuilles ;
  - **Versé → valeur (crypto)** : net versé depuis la banque vers les portefeuilles (toutes dates), comparé à la valeur
    actuelle, avec l'écart en euros.
- **Répartition** : un anneau avec deux vues au choix.
  - *Par endroit* : chaque livret et chaque portefeuille.
  - *Par actif* :
    - les livrets sont regroupés en une part « Livrets (euros) » ;
    - pour la crypto, on distingue « SOL en staking », « SOL disponible », puis chaque jeton ;
    - au-delà de 6 parts, les plus petites sont regroupées en « Autres ».
- **Évolution** : courbe empilée sur la période, avec une couche « Livrets » et une couche « Crypto ». La couche crypto
  vaut 0 et reste invisible avant le premier relevé. Une note sous la courbe le précise : « Crypto suivie depuis le
  JJ/MM/AAAA ».
- **Où** : une carte par endroit, avec :
  - le nom et la valeur à la fin de la période ;
  - la variation sur la période quand elle est calculable ;
  - pour un portefeuille, le net versé et l'écart.
  Un clic sur une carte de livret ouvre le détail de sa catégorie (comportement actuel). Un clic sur une carte de
  portefeuille ouvre l'onglet Banque, section portefeuilles.
- **Le tableau existant « Par destination »** (versements, retraits, net sur la période) est conservé **sous** les
  cartes, dans une section repliable « Mouvements de la période ». *(choix du concepteur : ces flux restent utiles et
  existent déjà)*

**Non-objectifs**
- Reconstituer la valeur passée de la crypto (décision de l'utilisateur).
- Déplacer la gestion des portefeuilles : la clé Helius, l'ajout et le retrait d'adresses, et le détail des positions
  restent dans l'onglet Banque (proposition non contestée par l'utilisateur).
- Valoriser une destination sans suivi : Kraken, ou tout virement « Épargne / X » sans compte ni portefeuille suivi.
- Toucher aux totaux de la vue d'ensemble (Dépenses, Revenus, Épargne nette restent des flux bancaires).

**Anti-objectifs**
- **Fausse hausse** : l'arrivée de la crypto dans la courbe ne doit pas passer pour une hausse de l'épargne (§ 5.4).
- **Valeur fausse en silence** (déjà dans la spec Solana) :
  - un portefeuille sans relevé, ou des positions sans prix, sont signalés et jamais comptés pour 0 sans le dire ;
  - un livret dont le solde est inconnu à une date ne vaut pas 0.
- **Masquage contourné** : tous les montants du tableau de bord suivent le mode discret, y compris les axes et les
  info-bulles des graphiques. Les chiffres en euros de l'onglet Épargne suivent aujourd'hui le type « savings » ; les
  avoirs utilisent le type « balance ».

## 4. Approches envisagées pour la courbe

| Approche | Pour | Contre |
|---|---|---|
| **1. Empilé livrets + crypto** (retenue) | Utile tout de suite : l'historique des livrets est complet, et l'arrivée de la crypto se voit et se comprend. | Le total fait un saut le jour du premier relevé crypto ; ce saut est visible et expliqué. |
| 2. Tout depuis aujourd'hui | Homogène. | Courbe vide pendant des semaines, et l'historique des livrets est perdu. |
| 3. Deux courbes séparées | Aucun saut. | Pas de vue d'ensemble. |

## 5. Conception

### 5.1 Données : solde d'un compte d'épargne à une date

- **Comptes concernés** : `accounts.kind = 'savings'`, **tous**, quel que soit le sélecteur de comptes de la barre
  commune. Ce sélecteur reste appliqué au tableau des mouvements, comme aujourd'hui. *(choix du concepteur : « combien
  j'ai et où » porte sur toute l'épargne)*
- **Solde à la date `d`** = clôture (`closing_cents`) du dernier relevé `ok` du compte dont `statement_date <= d`, plus
  la somme des `amount_cents` des opérations du compte avec `statement_date < op_date <= d`. Ce calcul prend en compte
  les opérations venues d'Enable Banking après le dernier PDF (même frontière que `app/sources/service.py`).
- **Sans relevé antérieur à `d`** : solde **inconnu**. Le compte est exclu des sommes à cette date, et le KPI le
  signale (« hors N compte(s) sans solde connu »).
- Les jambes miroir (`mirror_of`) restent dans le solde : elles sont de vrais mouvements du livret.

### 5.2 Données : valeur crypto à une date

- **Valeur d'un portefeuille au jour `d`** : `total_eur_cents` du dernier relevé `ok` (`wallet_snapshots`) avec
  `taken_at <= fin du jour d` (UTC). Sans relevé `ok` antérieur, la valeur est inconnue ; avant le premier relevé, le
  portefeuille ne contribue pas.
- **Crypto à la date `d`** = somme des valeurs connues. Le **premier jour suivi** est le plus ancien `taken_at` d'un
  relevé `ok`, tous portefeuilles confondus.
- **Masquage** : on reprend les totaux stockés dans l'historique, figés au moment du relevé. La valeur **actuelle**
  (fin de période = aujourd'hui) reprend le total recalculé de `wallets.overview`, pour rester cohérente avec l'onglet
  Banque. *(choix du concepteur ; écart connu et accepté, déjà noté en revue finale Solana)*

### 5.3 Série de la courbe

- **Un point par jour** de la période, jusqu'à 400 jours. Au-delà, un point par semaine, valeur au dernier jour de la
  semaine. *(choix du concepteur)*
- Chaque point porte `{date, livrets, crypto}`, avec `crypto = null` avant le premier jour suivi.
- Les jours futurs (après aujourd'hui) sont exclus.

### 5.4 Variation sur la période (KPI « Épargne totale »)

- On compare **à périmètre constant** : la variation porte seulement sur ce qui avait une valeur connue **au début ET à
  la fin** de la période.
- Si la crypto n'a pas de valeur au début de la période, elle est exclue de la variation. Le KPI affiche alors
  « livrets seuls, crypto suivie depuis le JJ/MM ».
- Même règle par carte : la variation d'un endroit ne s'affiche que s'il a une valeur au début et à la fin.

### 5.5 API

Nouvelle route **`GET /api/tabs/epargne/dashboard?start=…&end=…`** (`start` et `end` obligatoires, comme ailleurs).
Le paramètre `accounts` n'est **pas** pris : le tableau de bord porte sur toute l'épargne (§ 5.1). La route existante
`/summary` est inchangée et sert au tableau des mouvements, qui garde le sélecteur de comptes. *(choix du concepteur)*

```
{
  "as_of": "2026-11-15",                // min(end, aujourd'hui)
  "total": {value, change, change_scope: "all"|"savings_only"|null},
  "savings": {value, count, unknown_count},
  "crypto": {value, count, unread_count, unpriced_count, deposited, gap, tracked_since},
  "places": [{kind: "savings"|"wallet", id, name, category_id, value, change, deposited, gap, unknown: bool}],
  "allocation": {"by_place": [{name, value}], "by_asset": [{name, value}]},
  "series": [{date, savings, crypto}]
}
```

- Montants en `Decimal` d'euros (`to_jsonable`).
- Une valeur inconnue vaut `null`, jamais 0.
- `by_asset` agrège les positions du dernier relevé `ok` de chaque portefeuille (hors mints masqués et hors positions
  sans prix) :
  - les comptes de staking forment « SOL en staking » ;
  - `kind = 'sol'` forme « SOL disponible » ;
  - chaque jeton est regroupé par mint, sous son symbole.

Code :
- calculs dans un nouveau module `app/analytics/holdings.py`, fonctions pures sur une connexion, testées ;
- route dans `app/tabs/epargne.py`.

### 5.6 Interface (`app/web/tabs/epargne.js`)

Disposition validée sur maquette (option A) :
1. **Ligne de 4 tuiles** : reprendre le style `tiles` existant.
2. **Ligne en deux colonnes**, qui passe en une colonne sur téléphone :
   - **Répartition** : anneau Chart.js (`doughnut`) et légende, avec un sélecteur « par endroit / par actif » ;
   - **Évolution** : courbe Chart.js empilée (`line`, `fill`, `stacked`), avec deux couches « Livrets » et « Crypto »
     et la note « Crypto suivie depuis… ».
3. **Cartes « Où »** : grille de cartes. Les endroits sans valeur connue affichent « valeur inconnue ».
4. **Section repliable « Mouvements de la période »** : les tuiles versements / retraits / net et le tableau par
   destination actuels, ainsi que l'ancien graphique net cumulé.

Autres règles :
- Montants via `money(v, "balance")` pour les avoirs ; les flux gardent « savings ».
- Couleurs : variables de thème existantes (`--chart-N`), lisibles en clair et en sombre.
- Si aucune donnée (pas de livret ni de portefeuille), l'onglet affiche un message d'accueil. Ce message renvoie vers
  l'onglet Banque pour ajouter un portefeuille, et vers le dépôt de relevés.

### 5.7 Section de l'onglet Banque

Elle reste en place pour la gestion :
- la clé Helius ;
- l'ajout, le renommage et le retrait d'adresses ;
- le détail des positions ;
- les validateurs et le masquage des jetons.

Le graphique d'historique par portefeuille y est **retiré**, puisque l'évolution vit désormais dans Épargne. Un lien
« Voir l'analyse dans Épargne » le remplace. *(choix du concepteur)*

### 5.8 Tests

- `app/analytics/holdings.py` :
  - solde d'un livret à une date (avec et sans relevé antérieur, opérations API après le dernier PDF) ;
  - valeur crypto à une date (report du dernier relevé, portefeuille non lu, plusieurs portefeuilles) ;
  - série journalière et hebdomadaire ;
  - variation à périmètre constant (crypto exclue avant suivi) ;
  - répartition par actif (regroupement « Autres », mints masqués exclus).
- Route : forme de la réponse, `null` pour l'inconnu, données fictives uniquement.
- Les tests existants de `/summary` restent verts.

### 5.9 Précisions issues de la relecture *(choix du concepteur)*

- **Livret et catégorie** : la carte d'un livret est reliée à sa sous-catégorie d'épargne par la logique de
  `savings_target.savings_category_for_account`, utilisée ici en **lecture seule**. On ajoute un paramètre
  `create=False` qui renvoie `None` au lieu de créer : une requête GET n'écrit rien. Sans catégorie trouvée, la carte
  n'est pas cliquable. Aucune migration.
- **Destination sans compte suivi** (Kraken, virement « Épargne / X » sans livret ni portefeuille) : pas de carte, car
  sa valeur est inconnue. Elle reste visible dans « Mouvements de la période ».
- **Livret apparu en cours de période** (premier relevé après `start`) : sa couche entre dans la courbe à cette date.
  Ce saut est réel : l'argent a été versé sur un nouveau compte. Il n'est pas compté dans la variation du KPI, qui se
  calcule **compte par compte** : seuls entrent dans la variation les endroits qui ont une valeur au début et à la fin.
- **« Valeur au début »** de la période = valeur à la fin du jour qui précède `start`. **Valeur à la fin** = valeur à
  la fin du jour `as_of`.
- **Répartition** : les deux vues de l'anneau, les KPI et les cartes portent tous sur `as_of`. `by_asset` utilise les
  positions du dernier relevé `ok` de chaque portefeuille **antérieur ou égal à `as_of`** (stockées dans
  `wallet_positions`), avec les mints masqués actuels.
- **Livrets** : `deposited` et `gap` valent `null`. Le « net versé » ne concerne que les portefeuilles.
- **Série** : elle commence à `start`. Les jours sans aucune valeur connue (ni livret ni crypto) ont
  `savings = null, crypto = null`, et Chart.js n'en trace rien.

## 6. Laissé au builder

- Détails visuels : tailles, espacements, couleurs exactes parmi les variables de thème, libellés courts.
- Requêtes SQL : une requête par compte ou une requête groupée, au choix (volume minuscule).
- Format de date des axes.
