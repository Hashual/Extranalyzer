# Extranalyzer : repères pour Claude

Application personnelle FastAPI + SQLite d'analyse de comptes bancaires, déployée en Docker sur un Raspberry Pi.
Un push sur `main` reconstruit l'image (GitHub Actions, `ghcr.io/hashual/extranalyzer`), ce qui revient à
redéployer le Pi : ne jamais pousser sans l'accord de l'utilisateur.

## Confidentialité

Ne jamais ouvrir, lire, afficher ni committer `extraits/` (vrais relevés), ni aucun `*.xlsx`, ni les clés de
`~/.extranalyzer-secrets`. Les tests et démos utilisent des données fictives.

## Réseau : lire `docs/reseau.md` avant toute question réseau

- Il y a **deux réseaux distincts** : WireGuard `10.10.0.0/24`, dont le serveur est **proxyServer** (VPS, `wg0`),
  et **WolfNet** `10.10.10.0/24` (WolfStack, `wolfnet0`), auquel appartient le **Pi**.
- **Il n'y a pas de WireGuard sur le Pi.** Le Pi est `10.10.10.95` (WolfNet) et `192.168.1.95` (LAN).
  Extranalyzer écoute sur `http://10.10.10.95:8000` (`BIND_ADDR=10.10.10.95`). Le `wg0`/`10.8.0.1` du README est
  un exemple générique, faux ici.
- `10.10.0.2` est le **PC Windows** (tunnel `Acces_wolfStack`), pas le Pi.
- Le « proxy » désigne la machine **proxyServer**, pas un proxy HTTP.
- SSH vers le Pi : `ssh -i ~/.ssh/extranalyzer_pi raspberry@10.10.10.95` ; vers le VPS : `ssh proxyServer`.
- Vérifier sur la machine avant d'affirmer quoi que ce soit, puis mettre à jour `docs/reseau.md`.

## Conventions

Commits en français, terminés par une ligne `Co-Authored-By` qui nomme le modèle. Venv :
`C:/Users/dodo2/Documents/Perso/Applications/Extranalyzer/.venv`.
