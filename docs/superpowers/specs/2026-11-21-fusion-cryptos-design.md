# Fusion manuelle de cryptos dans Épargne

## Intention

Voir une même exposition en une seule part quand elle vient de sources différentes, par exemple le WBTC d'un
portefeuille Solana et le BTC de Kraken. La fusion automatique ne couvre que les cryptos dont l'adresse Solana est
connue (SOL, USDC, USDT, EURC, JUP, BONK).

## Approches

- **Réutiliser les groupes existants** (choisie) : la table `crypto_groups` range déjà un actif dans un groupe
  nommé (« Altcoins »). Fusionner revient à mettre les deux actifs dans le groupe qui porte le nom de la cible.
  Rien à migrer.
- Table d'équivalences dédiée (actif → actif canonique) : un second mécanisme qui doublonne les groupes.
- Équivalences automatiques (WBTC = BTC, etc.) : les variantes (cbBTC, tBTC, stETH…) sont trop nombreuses et
  la décision revient à l'utilisateur.

## Comportement

- Carte « Répartition des cryptos » : chaque ligne a un menu « Fusionner avec… » qui liste les autres lignes
  (actifs ou groupes), plus « Altcoins » s'il n'existe pas encore.
  - Cible = actif hors groupe : les deux actifs rejoignent un groupe nommé comme la cible (« BTC (2) »).
  - Cible = actif déjà groupé, ou groupe : la source rejoint ce groupe.
  - Source = groupe : tous ses membres rejoignent la cible.
  - « Retirer » sur un membre défait la fusion pour lui.
- Les groupes sont exposés avec la clé `group:<nom>`, pour ne jamais entrer en collision avec un actif nommé comme
  le groupe (le BTC de Kraken a pour clé « BTC »).
- La vue « Par actif » de la carte « Répartition » utilise les mêmes catégories : SOL disponible et en staking
  ne font qu'une part, de même que les fusions et les groupes.
- Route : `POST /api/tabs/epargne/crypto/merge` {source, target, name}, en une transaction.
