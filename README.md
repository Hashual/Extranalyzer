# Extranalyzer

Application web personnelle d'analyse de relevés bancaires (FastAPI + SQLite), prévue pour tourner
sur un Raspberry Pi (linux/arm64), derrière WireGuard. Les relevés réels ne doivent **jamais** être
versionnés ni copiés dans l'image : ils sont déposés via l'interface web et vivent uniquement dans le
volume Docker `data`.

## Image Docker

- Build multi-stage sur `python:3.12-alpine` ; l'étape `build` n'accepte que des wheels binaires
  (`--only-binary=:all:`), aucune compilation. L'image finale ne contient ni pip-cache ni outils de build,
  tourne en utilisateur non privilégié (uid 10001) avec un système de fichiers racine en lecture seule.
- Contexte de build minimal (`.dockerignore` en liste blanche : `app/`, `pyproject.toml` et
  `constraints.txt` seulement).
- Dépendances figées : l'étape `build` installe les versions exactes de `constraints.txt` (voir
  « Dépendances figées »), deux builds du même commit donnent donc les mêmes paquets.
- Taille de l'image : environ 122 Mo en arm64 (voir « Tailles mesurées »).
- Repli : si une dépendance n'avait pas de wheel `musllinux` pour aarch64, passer les deux `FROM` du
  `Dockerfile` sur `python:3.12-slim` (non nécessaire à ce jour).

## Construire et installer l'image

Deux routes, au choix. Dans les deux cas, le Pi a besoin de Docker et de `docker-compose.yml` (un seul
fichier à copier, pas besoin du dépôt complet).

**Route A : construire sur le PC, envoyer l'image au Pi** (Docker Desktop, buildx, émulation QEMU) :

```bash
docker buildx build --platform linux/arm64 -t extranalyzer:arm64 --load .
docker save extranalyzer:arm64 | gzip > extranalyzer-arm64.tar.gz
scp extranalyzer-arm64.tar.gz docker-compose.yml pi@<ip-du-pi>:~/extranalyzer/
# sur le Pi :
cd ~/extranalyzer && gunzip -c extranalyzer-arm64.tar.gz | docker load
```

**Route B : construire sur le Pi** (dépôt cloné sur le Pi) :

```bash
git clone <url-du-depot> ~/extranalyzer && cd ~/extranalyzer
docker build -t extranalyzer:arm64 .
```

## Déployer (sur le Pi, dans le dossier contenant `docker-compose.yml`)

1. Créer le secret (dossier ignoré par git, jamais commité). Sous Linux, le secret du compose est monté
   avec le propriétaire et les droits du fichier hôte : le conteneur tourne en uid 10001, il faut donc
   que ce soit lui le propriétaire, sinon l'application ne peut pas le lire et redémarre en boucle.

   ```bash
   mkdir -p secrets && chmod 700 secrets
   printf '%s' 'votre-mot-de-passe-long' > secrets/password.txt
   sudo chown 10001:10001 secrets/password.txt && sudo chmod 400 secrets/password.txt
   ```

   (Le dossier `secrets/` en 700 appartient à votre utilisateur : il reste traversable par Docker, qui
   lit le fichier en root.)

   Créer aussi `secrets/gmail_app_password.txt` : `docker compose` exige que ce fichier existe, même
   vide. Un fichier vide désactive la relève des tickets de caisse ; pour l'activer, voir « Tickets de
   caisse ».

   ```bash
   : > secrets/gmail_app_password.txt
   sudo chown 10001:10001 secrets/gmail_app_password.txt && sudo chmod 400 secrets/gmail_app_password.txt
   ```

2. Choisir l'adresse d'écoute dans un fichier `.env` à côté du compose (ignoré par git ; ainsi
   `docker compose stop/ps/down` fonctionnent aussi dans une nouvelle session SSH) :
   `BIND_ADDR` = l'IP WireGuard du Pi (par exemple `10.8.0.1`), ou son IP LAN. Elle est obligatoire :
   `docker compose` refuse de démarrer sans elle. Ne jamais mettre `0.0.0.0`.

   ```bash
   echo 'BIND_ADDR=10.8.0.1' > .env
   ```

   `IMAGE_TAG` (facultatif, `arm64` par défaut) permet d'utiliser une autre étiquette d'image, par
   exemple `IMAGE_TAG=amd64` pour un essai sur un PC x86.

3. Démarrer :

   ```bash
   docker compose up -d
   docker compose ps          # l'état doit passer à « healthy »
   ```

4. Ouvrir `http://<BIND_ADDR>:8000` depuis un appareil connecté au VPN WireGuard.

Le conteneur est durci : système de fichiers en lecture seule (écriture uniquement dans le volume
`/data` et le tmpfs `/tmp` de 64 Mo), `cap_drop: ALL`, `no-new-privileges`, 256 Mo de mémoire, 1 CPU,
journaux limités (`max-size: 1m`, `max-file: "3"`). Variable facultative : `EXTRANALYZER_MAX_UPLOAD_MB`
(20 par défaut).

### Démarrage au boot : attendre WireGuard

Docker ne peut pas publier le port sur l'IP `wg0` tant que l'interface n'existe pas. Méthode recommandée :
faire démarrer le service Docker après `wg-quick@wg0`.

```bash
sudo systemctl edit docker.service
```

puis y coller :

```ini
[Unit]
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
```

(Sauvegarder, puis `sudo systemctl daemon-reload`.) Avec `restart: unless-stopped`, le conteneur redémarre
alors tout seul après un reboot, une fois `wg0` monté.

### Dépannage

- `lecture impossible de EXTRANALYZER_PASSWORD_FILE` dans `docker compose logs` : le secret n'est pas
  lisible par l'uid 10001 ; refaire le `chown 10001:10001` et `chmod 400` ci-dessus, puis
  `docker compose up -d --force-recreate`.
- `required variable BIND_ADDR is missing` : le fichier `.env` est absent ou ne se trouve pas à côté du compose.
- `secrets/gmail_app_password.txt` introuvable au `docker compose up` : créer le fichier (vide pour
  désactiver la relève, voir « Tickets de caisse »).
- `lecture impossible de EXTRANALYZER_GMAIL_APP_PASSWORD_FILE` dans `docker compose logs` : même cause
  que pour le mot de passe, refaire le `chown 10001:10001` et `chmod 400` sur ce fichier.

## Tickets de caisse

L'application peut relever les tickets de caisse (PDF) reçus par mail dans Gmail, puis les rapprocher
des débits de carte (onglet Courses). La relève est désactivée tant que le mot de passe d'application
ou `GMAIL_USER` manque.

1. Sur le compte Google, activer la validation en deux étapes, puis créer un mot de passe
   d'application (compte Google, Sécurité, Mots de passe des applications). Google l'affiche par groupes
   de 4 caractères : les espaces sont retirés par l'application, on peut le coller tel quel.
2. Sur le Pi, l'écrire dans le secret, avec les mêmes droits que le mot de passe de l'application :

   ```bash
   printf '%s' 'xxxx xxxx xxxx xxxx' > secrets/gmail_app_password.txt
   sudo chown 10001:10001 secrets/gmail_app_password.txt && sudo chmod 400 secrets/gmail_app_password.txt
   ```

3. Ajouter l'adresse Gmail dans le `.env` (ignoré par git), puis recréer le conteneur :

   ```bash
   echo 'GMAIL_USER=votre.adresse@gmail.com' >> .env
   docker compose up -d --force-recreate
   ```

Ce que fait la relève :

- Gmail est ouvert en **lecture seule** : aucun mail n'est marqué lu, déplacé ni supprimé.
- Elle lit deux libellés Gmail, par défaut `Super U/Tickets de caisses` (enseigne `superu`) et
  `intermarche/Tickets de caisses` (enseigne `intermarche`), une fois toutes les heures et au démarrage ;
  le bouton « Relever maintenant » de l'onglet Courses la lance à la demande.
- Les PDF sont rangés dans `receipts/` du volume `data`.
- La seule connexion sortante est `imap.gmail.com:993` (TLS vérifié).
- Les journaux ne contiennent ni adresse, ni mot de passe, ni libellé de produit, ni montant.

Variables facultatives (dans `.env` à côté du compose) : `RECEIPT_LABELS` (format
`libellé=enseigne;libellé=enseigne`, les deux libellés ci-dessus par défaut) et, dans le service,
`EXTRANALYZER_MAIL_INTERVAL_MIN` (60 par défaut, entier positif ; à ajouter à `environment:` du compose
pour le changer).

Désactiver la relève : vider le secret (`sudo sh -c ': > secrets/gmail_app_password.txt'`, droits
inchangés) ou laisser `GMAIL_USER` vide, puis `docker compose up -d --force-recreate`. Les tickets déjà
récupérés restent dans l'application.

## Sauvegarde et restauration du volume `data`

Le nom réel du volume est préfixé par le projet (`docker volume ls`, souvent `extranalyzer_data`).

```bash
docker compose stop
docker run --rm -v extranalyzer_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/extranalyzer-data-$(date +%F).tar.gz -C /data .
docker compose start
```

Restauration (pile arrêtée, volume créé s'il manque, puis redémarrage) :

```bash
docker compose down
docker volume create extranalyzer_data
docker run --rm -v extranalyzer_data:/data -v "$PWD":/backup alpine \
  sh -c "tar xzf /backup/extranalyzer-data-AAAA-MM-JJ.tar.gz -C /data && chown -R 10001:10001 /data"
docker compose up -d
```

L'archive contient les PDF des relevés, les PDF des tickets de caisse (`receipts/`) et la base : la ranger hors du dépôt, chiffrée si elle quitte le Pi.

## Mettre à jour

- **Route A** (build sur le PC) : refaire le `buildx`, le `docker save` et le `scp` ci-dessus, puis sur le Pi :
  `gunzip -c extranalyzer-arm64.tar.gz | docker load && docker compose up -d`.
- **Route B** (build sur le Pi) : `git pull && docker build -t extranalyzer:arm64 . && docker compose up -d`.

`docker compose up -d` recrée le conteneur ; le volume `data` est conservé.

## Développement local

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

## Dépendances figées

`constraints.txt` fixe la version exacte de chaque paquet de l'image : outils de build (`setuptools`,
`wheel`), dépendances de `pyproject.toml` et toutes leurs dépendances transitives. Le `Dockerfile`
l'applique (`-c constraints.txt`) ; `pyproject.toml` garde des dépendances sans version pour le
développement local. `tests/test_constraints.py` vérifie que rien n'y manque et que le `Dockerfile`
s'en sert.

Chaque version doit exister en wheel pour linux/arm64 musl (`python:3.12-alpine`), sans quoi le build
échoue (`--only-binary=:all:`). Pour mettre à jour :

1. Mettre à jour le venv (`.venv/Scripts/python -m pip install -U -e ".[dev]"`) et faire passer les tests.
2. Reporter dans `constraints.txt` les versions installées (`.venv/Scripts/python -m pip freeze`),
   sans `extranalyzer` ni les paquets de développement (`pytest`, `httpx2`, `reportlab`…).
3. Vérifier, sans Docker, que tout se résout en wheels pour l'image (les deux étiquettes `musllinux`
   sont nécessaires : certains paquets, comme `pydantic_core`, ne publient que `musllinux_1_1`) :

   ```bash
   .venv/Scripts/python -m pip install --dry-run --ignore-installed --target .tmp-check -c constraints.txt --only-binary=:all: --platform musllinux_1_2_aarch64 --platform musllinux_1_1_aarch64 --python-version 3.12 --implementation cp --abi cp312 setuptools wheel fastapi uvicorn python-multipart pdfplumber itsdangerous
   ```

   La liste « Would install » doit reprendre exactement les versions de `constraints.txt`.
4. Reconstruire l'image (« Construire et installer l'image ») et vérifier qu'elle passe « healthy ».

## Tailles mesurées

Mesurées avec `docker image ls extranalyzer` :

| Plateforme   | Taille de l'image |
|--------------|-------------------|
| linux/arm64  | 122 Mo            |
| linux/amd64  | 117 Mo            |

Mémoire au repos (conteneur démarré, un relevé importé) : environ 48 Mo.
