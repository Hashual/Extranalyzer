# Extranalyzer : repères pour Claude

Application personnelle FastAPI + SQLite d'analyse de comptes bancaires, déployée en Docker sur un Raspberry Pi.
Un push sur `main` reconstruit l'image (GitHub Actions, `ghcr.io/hashual/extranalyzer`), ce qui revient à
redéployer le Pi : ne jamais pousser sans l'accord de l'utilisateur.

## Confidentialité

Ne jamais ouvrir, lire, afficher ni committer `extraits/` (vrais relevés), ni aucun `*.xlsx`, ni les clés de
`~/.extranalyzer-secrets`. Les tests et démos utilisent des données fictives.

## Réseau : lire `docs/reseau.md` avant toute question réseau

- Il y a **deux réseaux distincts** : WireGuard `<réseau WireGuard>`, dont le serveur est **<VPS>** (VPS, `wg0`),
  et **un VPN maillé** `<réseau VPN>` (<plateforme VPN>, `<interface VPN>`), auquel appartient le **Pi**.
- **Il n'y a pas de WireGuard sur le Pi.** Le Pi est `<BIND_ADDR>` (un VPN maillé) et `<IP LAN>` (LAN).
  Extranalyzer écoute sur `http://<BIND_ADDR>:8000` (`BIND_ADDR=<BIND_ADDR>`). Le `wg0`/`10.8.0.1` du README est
  un exemple générique, faux ici.
- `<BIND_ADDR>` est le **PC Windows** (tunnel `<tunnel>`), pas le Pi.
- Le « proxy » désigne la machine **<VPS>**, pas un proxy HTTP.
- SSH vers le Pi : `ssh -i <clé> <utilisateur>@<BIND_ADDR>` ; vers le VPS : `ssh <VPS>`.
- Vérifier sur la machine avant d'affirmer quoi que ce soit, puis mettre à jour `docs/reseau.md`.

## Conventions

Commits en français, terminés par une ligne `Co-Authored-By` qui nomme le modèle. Venv :
`C:/Users/<utilisateur>/Documents/Perso/Applications/Extranalyzer/.venv`.
