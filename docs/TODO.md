# À faire

## Épargne : plus-value (PNL) par catégorie de crypto (en pause)

Afficher, pour chaque catégorie de la répartition des cryptos (actif seul ou groupe « Altcoins »), la valeur
actuelle face au montant investi, avec la plus-value en € et en %, en vert ou en rouge.

Le montant investi pourrait être saisi à la main par catégorie, ou calculé (au moins pré-rempli) :

- **Historique des swaps** : l'API de transactions enrichies de Helius (`/v0/addresses/{adresse}/transactions`,
  type `SWAP`) donne, pour chaque swap, ce qui a été donné et reçu (ex. 2 SOL → 1 500 JUP).
- **Cours historique en euros** : Helius ne le fournit pas. Il faut un second service (ex. CoinGecko, gratuit avec
  limites) pour valoriser le SOL ou l'USDC dépensé au jour du swap.
- **Règle de calcul** : prix moyen pondéré par jeton ; une vente partielle réduit le montant investi au prorata ;
  swap jeton → jeton = vente de l'un et achat de l'autre.
- **Trous connus** : jetons achetés sur un exchange puis envoyés au portefeuille, airdrops, récompenses de staking :
  aucun prix d'achat on-chain. La saisie manuelle doit rester possible et l'emporter sur le calcul.
