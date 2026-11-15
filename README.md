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

Trois routes, au choix. Dans tous les cas, le Pi a besoin de Docker et de `docker-compose.yml` (un seul
fichier à copier, pas besoin du dépôt complet).

**Route GitHub (recommandée) : l'image est construite par GitHub Actions, le Pi la télécharge.** Le dépôt
doit être **privé**. À chaque push sur `main`, le workflow `.github/workflows/docker.yml` lance les tests puis
publie l'image `linux/arm64` sur `ghcr.io/<compte>/<dépôt>` (étiquettes `arm64` et `arm64-<commit>`), sans
secret à configurer. Le paquet ghcr.io créé par un dépôt privé est privé lui aussi. Une seule fois :

1. Sur GitHub, créer un jeton classique (*Settings → Developer settings → Personal access tokens →
   Tokens (classic)*) avec la seule permission `read:packages`.
2. Sur le Pi, s'identifier (le jeton est demandé comme mot de passe, il n'apparaît pas dans l'historique) :

   ```bash
   docker login ghcr.io -u <compte-github>
   ```

3. Ajouter au `.env` du Pi (étape 2 ci-dessous) le nom de l'image, **en minuscules** :

   ```bash
   echo 'IMAGE=ghcr.io/<compte>/<depot>' >> .env
   ```

Puis `docker compose pull && docker compose up -d`. Le Pi ne reçoit que l'image arm64, jamais le code.

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

   `IMAGE` (facultatif, `extranalyzer` par défaut : image construite en local) donne le nom de l'image,
   `ghcr.io/<compte>/<depot>` pour la route GitHub. `IMAGE_TAG` (facultatif, `arm64` par défaut) permet
   d'utiliser une autre étiquette, par exemple `IMAGE_TAG=arm64-<commit>` pour revenir à une version
   précise, ou `IMAGE_TAG=amd64` pour un essai sur un PC x86.

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

> Le Pi actuel n'utilise pas WireGuard mais un VPN maillé (`<BIND_ADDR>`, `<service VPN>`). Son réseau réel est décrit
> dans [docs/reseau.md](docs/reseau.md).

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

### Récupération bancaire (Enable Banking)

Facultative : sans elle, l'appli fonctionne avec les seuls relevés PDF. Avec elle, les opérations des comptes
arrivent seules, via l'open banking (DSP2, lecture seule), **après le dernier relevé PDF de chaque compte**.
Conception : `docs/superpowers/specs/2026-11-12-enable-banking-design.md`.

1. **Importer d'abord tous les relevés PDF** jusqu'au plus récent : la banque ne fournit souvent que 90 jours
   d'historique, et un relevé dont la période contient déjà des opérations de la banque est refusé.
2. Créer un compte sur enablebanking.com, puis une application **Production** dans le Control Panel
   (`https://enablebanking.com/cp/applications`) :
   - adresse de retour (liste blanche) : `http://<BIND_ADDR>:8000/bank/callback` ; si elle est refusée,
     déclarer par exemple `https://localhost/bank/callback` et utiliser le repli par copier-coller (étape 6) ;
   - la clé privée est téléchargée par le navigateur sous le nom `<identifiant>.pem` ;
   - « Activate by linking accounts » : lier ses propres comptes Crédit Mutuel (mode restreint, usage
     personnel). Vérifier au passage les conditions tarifaires affichées.
3. Déposer la clé en secret, lisible par l'uid du conteneur :

   ```bash
   cp <identifiant>.pem secrets/enablebanking.pem
   sudo chown 10001:10001 secrets/enablebanking.pem && sudo chmod 400 secrets/enablebanking.pem
   ```

4. Compléter `.env` :

   ```bash
   cat >> .env <<'EOF'
   COMPOSE_FILE=docker-compose.yml:docker-compose.banque.yml
   EB_APP_ID=<identifiant de l'application>
   EB_REDIRECT_URL=http://10.8.0.1:8000/bank/callback
   EOF
   docker compose up -d
   ```

   Facultatif : `SYNC_HOURS` (12 par défaut ; 0 désactive la synchro planifiée, le bouton reste) et
   `EB_ASPSP` (banque proposée par défaut dans la liste d'Enable Banking, « Crédit Mutuel » par défaut).
   Le conteneur doit pouvoir joindre `https://api.enablebanking.com` en sortie.
5. Onglet **Banque** → « Connecter une banque » : choisir le pays et la banque dans la liste d'Enable Banking,
   authentification sur le site de la banque, retour dans l'appli, rattachement des comptes (automatique quand
   l'IBAN contient le numéro des relevés) et première synchro. Regarder ensuite le contrôle de solde de chaque compte.
   Plusieurs banques peuvent être connectées (« Ajouter une banque ») : chacune a son consentement, son
   renouvellement et sa déconnexion ; la synchro planifiée les parcourt toutes, la panne de l'une n'arrête pas les
   autres. En mode restreint, chaque nouvelle banque doit aussi être liée dans le Control Panel (« Link accounts »).
   Les comptes d'une banque sans relevé PDF sont créés « API seulement » ; leur type (courant ou épargne) est déduit
   de la banque et de leur nom, et se corrige dans l'onglet.
6. Repli : si la page de retour ne s'ouvre pas, copier l'adresse complète affichée par le navigateur (avec
   `code=` et `state=`) dans « Le retour de la banque n'a pas abouti ? », dans les 30 minutes.

Le consentement dure en général 180 jours ; une pastille « Banque : renouveler » apparaît 14 jours avant
l'échéance de la première banque concernée (« Renouveler le consentement » dans l'onglet). La clé ne quitte jamais le secret Docker ; aucun jeton,
identifiant de session ni IBAN complet n'est journalisé ou affiché.

### Dépannage

- Onglet Banque « non configurée » alors que `.env` est rempli : `docker compose logs` indique la cause
  (`clé privée illisible` : refaire le `chown`/`chmod` de `secrets/enablebanking.pem`).
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
2. Sur le Pi, l'écrire dans le secret (le fichier existe déjà, propriété de l'uid 10001 en lecture seule :
   il faut donc passer par `sudo tee`), en gardant les mêmes droits que le mot de passe de l'application :

   ```bash
   printf '%s' 'xxxx xxxx xxxx xxxx' | sudo tee secrets/gmail_app_password.txt >/dev/null
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
- La seule connexion sortante de la relève est `imap.gmail.com:993` (TLS vérifié).
- Les journaux ne contiennent ni adresse, ni mot de passe, ni libellé de produit, ni montant.

Variables facultatives (dans `.env` à côté du compose) : `RECEIPT_LABELS` (format
`libellé=enseigne;libellé=enseigne`, les deux libellés ci-dessus par défaut) et, dans le service,
`EXTRANALYZER_MAIL_INTERVAL_MIN` (60 par défaut, entier positif ; à ajouter à `environment:` du compose
pour le changer).

Désactiver la relève : vider le secret (`sudo sh -c ': > secrets/gmail_app_password.txt'`, droits
inchangés) ou laisser `GMAIL_USER` vide (supprimer la ligne du `.env` ou la mettre à `GMAIL_USER=`), puis `docker compose up -d --force-recreate`. Les tickets déjà
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

- **Première mise à jour vers la relève des tickets** (toutes routes) : le compose déclare maintenant le secret
  `secrets/gmail_app_password.txt`, et `docker compose up -d` échoue tant qu'il n'existe pas. Avant de
  mettre à jour, copier le nouveau `docker-compose.yml` sur le Pi (l'image publiée ne le contient pas ;
  route B : il arrive avec le `git pull`), puis créer le secret (vide : la relève reste désactivée, voir
  « Tickets de caisse » pour l'activer) :

  ```bash
  sudo sh -c ': > secrets/gmail_app_password.txt'
  sudo chown 10001:10001 secrets/gmail_app_password.txt && sudo chmod 400 secrets/gmail_app_password.txt
  ```

  Au premier démarrage, la base passe d'elle-même au schéma 7 (tables des tickets) ; rien d'autre à faire.
- **Route GitHub** : pousser sur `main`, attendre la fin du workflow (onglet *Actions*), puis sur le Pi :
  `cd ~/extranalyzer && docker compose pull && docker compose up -d`.
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
