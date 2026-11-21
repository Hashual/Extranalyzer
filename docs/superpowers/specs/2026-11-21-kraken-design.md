# Compte Kraken : conception (courte)

## Intention

Relier son compte Kraken depuis l'onglet Connexions, sans rien configurer sur le serveur : coller une clé API en
lecture seule, puis voir le compte et ses avoirs en euros dans l'onglet Épargne, à côté des portefeuilles Solana.

## Objectifs, non-objectifs

- Objectifs : saisie de la clé dans l'interface (comme Helius), relevé des soldes (`Balance`) valorisés en euros,
  actualisation manuelle et planifiée, intégration au bloc Crypto, à la courbe et à la répartition des cryptos.
- Non-objectifs : historique (`Ledgers`, `TradesHistory`) et plus-value, en pause (voir `docs/TODO.md`) ;
  plusieurs comptes Kraken (une clé à la fois) ; clé fournie par un secret Docker.
- Anti-objectifs : une clé capable de trader ou de retirer ; une clé réaffichée, journalisée ou commitée.

## Approches

- **A, retenue** : un compte Kraken est une ligne de `wallets` (`provider = 'kraken'`, migration v12) ; ses avoirs
  sont des relevés `wallet_snapshots` / `wallet_positions`. Épargne, courbe, répartition et « net versé » (virements
  classés dans la catégorie d'épargne « Kraken ») fonctionnent sans requête nouvelle.
- B : tables `exchange_*` séparées ; même affichage, mais chaque requête de `holdings.py` serait doublée.
- C : `accounts.kind = 'exchange'` alimenté par `Ledgers` ; mélange les cryptos aux opérations bancaires et ne donne
  pas de valeur du jour. Écartée.

## Décisions (validées avec l'utilisateur)

1. Solde actuel seulement (`Balance`).
2. Staking et Earn comptés (suffixes `.S`, `.M`, `.F`, `.B`, `.P`, `.T`) : position « en staking ».
3. Une même crypto à deux endroits fait une seule part dans la répartition : SOL de Kraken avec le SOL des
   portefeuilles, USDC/USDT/EURC… avec le jeton Solana correspondant.
4. Les euros laissés sur Kraken comptent dans le bloc Crypto (ligne « EUR (espèces) »).
5. Un seul compte Kraken.

## Comment (choix du constructeur)

- `app/sources/kraken/` : `client.py` (signature HMAC-SHA512, nonce croissant, transport injectable),
  `holdings.py` (normalisation des actifs, prix), `runtime.py` (clé dans `data_dir/kraken.key`, droits 600, JSON).
- Prix : endpoints publics `AssetPairs` + `Ticker`, paire en euros, sinon paire en dollars convertie par le cours
  EUR/USD ; sinon position sans prix (jamais inventée). Euros : prix 1.
- Positions : `kind = 'sol'` (SOL disponible), `'stake'` (actif placé), `'token'` (le reste). `mint` = clé d'actif :
  adresse Solana connue (SOL, USDC, USDT, EURC, JUP, BONK), sinon le symbole (`BTC`, `ETH`, `EUR`).
  `price_source = 'kraken'`.
- Routes : `/api/tabs/banque/kraken` (GET, PUT/DELETE `key`, POST `refresh`). Retirer la clé garde le compte et son
  historique ; « Retirer » le compte (route des portefeuilles) supprime l'historique.
- Tests : `tests/fakekraken.py`, faux transport REST rejouant des réponses fictives.
