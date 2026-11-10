# Extranalyzer

Application web personnelle d'analyse de relevés bancaires (FastAPI + SQLite), prévue pour tourner
sur un Raspberry Pi (linux/arm64), derrière WireGuard. Les relevés réels ne doivent **jamais** être
versionnés ni copiés dans l'image : ils sont déposés via l'interface web et vivent uniquement dans le
volume Docker `data`.

## Image Docker

- Build multi-stage sur `python:3.12-alpine` ; l'étape `build` n'accepte que des wheels binaires
  (`--only-binary=:all:`), aucune compilation. L'image finale ne contient ni pip-cache ni outils de build,
  tourne en utilisateur non privilégié (uid 10001) avec un système de fichiers racine en lecture seule.
- Contexte de build minimal (`.dockerignore` en liste blanche : `app/` et `pyproject.toml` seulement).
- Taille de l'image : environ 122 Mo en arm64 (voir « Tailles mesurées »).
- Repli : si une dépendance n'avait pas de wheel `musllinux` pour aarch64, passer les deux `FROM` du
  `Dockerfile` sur `python:3.12-slim` (non nécessaire à ce jour).

## Construire l'image

Sur le PC (Docker Desktop, buildx, émulation QEMU) :

```bash
docker buildx build --platform linux/arm64 -t extranalyzer:arm64 --load .
docker save extranalyzer:arm64 | gzip > extranalyzer-arm64.tar.gz
scp extranalyzer-arm64.tar.gz pi@<ip-du-pi>:~/
# sur le Pi :
gunzip -c extranalyzer-arm64.tar.gz | docker load
```

Ou directement sur le Pi (`docker compose build`, ou la commande `docker build -t extranalyzer:arm64 .`).

## Déployer

1. Copier `docker-compose.yml` (et le dépôt, ou au moins ce fichier) sur le Pi.
2. Créer le secret (fichier ignoré par git, jamais commité) :

   ```bash
   mkdir -p secrets
   printf '%s' 'votre-mot-de-passe-long' > secrets/password.txt
   chmod 600 secrets/password.txt
   ```

3. Choisir l'adresse d'écoute : `BIND_ADDR` = l'IP WireGuard du Pi (par exemple `10.8.0.1`), ou son IP LAN.
   Elle est obligatoire : `docker compose` refuse de démarrer sans elle. Ne jamais mettre `0.0.0.0`.

   ```bash
   export BIND_ADDR=10.8.0.1
   docker compose up -d
   docker compose ps          # l'état doit passer à « healthy »
   ```

4. Ouvrir `http://<BIND_ADDR>:8000` depuis un appareil connecté au VPN WireGuard.

Le conteneur est durci : système de fichiers en lecture seule (écriture uniquement dans le volume
`/data` et le tmpfs `/tmp`), `cap_drop: ALL`, `no-new-privileges`, 256 Mo de mémoire, 1 CPU, journaux
limités. Variables facultatives : `EXTRANALYZER_MAX_UPLOAD_MB` (20 par défaut).

## Sauvegarde et restauration du volume `data`

Le nom réel du volume est préfixé par le projet (`docker volume ls`, souvent `extranalyzer_data`).

```bash
docker compose stop
docker run --rm -v extranalyzer_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/extranalyzer-data-$(date +%F).tar.gz -C /data .
docker compose start

# restauration (volume vide) :
docker run --rm -v extranalyzer_data:/data -v "$PWD":/backup alpine \
  sh -c "tar xzf /backup/extranalyzer-data-AAAA-MM-JJ.tar.gz -C /data && chown -R 10001:10001 /data"
```

L'archive contient les PDF et la base : la ranger hors du dépôt, chiffrée si elle quitte le Pi.

## Mettre à jour

```bash
git pull
docker buildx build --platform linux/arm64 -t extranalyzer:arm64 --load .   # ou sur le Pi
docker compose up -d        # recrée le conteneur ; le volume `data` est conservé
```

## Développement local

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

## Tailles mesurées

Mesurées avec `docker image ls extranalyzer` :

| Plateforme   | Taille de l'image |
|--------------|-------------------|
| linux/arm64  | 122 Mo            |
| linux/amd64  | 117 Mo            |

Mémoire au repos (conteneur démarré, un relevé importé) : environ 48 Mo.
