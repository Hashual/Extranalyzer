# Extranalyzer : repères pour Claude

Application personnelle FastAPI + SQLite d'analyse de comptes bancaires, déployée en Docker sur un Raspberry Pi.
Un push sur `main` reconstruit l'image (GitHub Actions, `ghcr.io/hashual/extranalyzer`), ce qui revient à
redéployer le Pi : ne jamais pousser sans l'accord de l'utilisateur.

## Confidentialité

Ne jamais ouvrir, lire, afficher ni committer `extraits/` (vrais relevés), ni aucun `*.xlsx`, ni les clés de
`~/.extranalyzer-secrets`. Les tests et démos utilisent des données fictives.

Le dépôt est **public** : ne jamais committer d'adresse IP, de nom d'hôte, d'utilisateur, de nom de clé SSH ni
aucun autre détail de l'infrastructure. Ils vivent dans des fichiers locaux ignorés par git.

## Réseau

Les repères réseau sont dans `CLAUDE.local.md` et `docs/reseau.local.md` (non versionnés) : les lire avant
toute question réseau. Dans les fichiers versionnés, utiliser des exemples génériques (`<BIND_ADDR>`, `10.8.0.1`).

## Conventions

Commits en français, terminés par une ligne `Co-Authored-By` qui nomme le modèle. Venv :
`C:/Users/<utilisateur>/Documents/Perso/Applications/Extranalyzer/.venv`.
