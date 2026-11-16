# Extranalyzer

Personal web application for analyzing bank statements (FastAPI + SQLite), designed to run on a
Raspberry Pi (linux/arm64) behind WireGuard. Real statements must **never** be committed or copied into
the image: they are uploaded through the web interface and live only in the `data` Docker volume.

> The application's interface is in French. In this guide, interface labels are quoted in French, with
> an English translation where helpful.

## Docker image

- Multi-stage build on `python:3.12-alpine`; the `build` stage only accepts binary wheels
  (`--only-binary=:all:`), with no compilation. The final image contains no pip cache or build tools, runs
  as an unprivileged user (uid 10001) and uses a read-only root filesystem.
- Minimal build context (`.dockerignore` as an allowlist: only `app/`, `pyproject.toml` and
  `constraints.txt`).
- Pinned dependencies: the `build` stage installs the exact versions from `constraints.txt` (see
  "Pinned dependencies"), so two builds of the same commit produce the same packages.
- Image size: about 122 MB on arm64 (see "Measured sizes").
- Fallback: if a dependency had no `musllinux` wheel for aarch64, switch both `FROM` lines in the
  `Dockerfile` to `python:3.12-slim` (not needed so far).

## Building and installing the image

There are three options. In every case, the Pi needs Docker and `docker-compose.yml` (a single file to
copy; the full repository is not needed).

**GitHub option (recommended): GitHub Actions builds the image and the Pi downloads it.** The repository
must be **private**. On every push to `main`, the `.github/workflows/docker.yml` workflow runs the tests,
then publishes the `linux/arm64` image to `ghcr.io/<account>/<repository>` (tags `arm64` and
`arm64-<commit>`), with no secret to configure. A ghcr.io package created by a private repository is
private too. One-time setup:

1. On GitHub, create a classic token (*Settings → Developer settings → Personal access tokens →
   Tokens (classic)*) with only the `read:packages` permission.
2. On the Pi, log in (the token is requested as the password, so it does not appear in the shell
   history):

   ```bash
   docker login ghcr.io -u <github-account>
   ```

3. Add the image name, **in lowercase**, to the Pi's `.env` (step 2 below):

   ```bash
   echo 'IMAGE=ghcr.io/<account>/<repository>' >> .env
   ```

Then run `docker compose pull && docker compose up -d`. The Pi only receives the arm64 image, never the
code.

**Option A: build on the PC and send the image to the Pi** (Docker Desktop, buildx, QEMU emulation):

```bash
docker buildx build --platform linux/arm64 -t extranalyzer:arm64 --load .
docker save extranalyzer:arm64 | gzip > extranalyzer-arm64.tar.gz
scp extranalyzer-arm64.tar.gz docker-compose.yml pi@<pi-ip>:~/extranalyzer/
# on the Pi:
cd ~/extranalyzer && gunzip -c extranalyzer-arm64.tar.gz | docker load
```

**Option B: build on the Pi** (repository cloned on the Pi):

```bash
git clone <repository-url> ~/extranalyzer && cd ~/extranalyzer
docker build -t extranalyzer:arm64 .
```

## Deploying (on the Pi, in the folder containing `docker-compose.yml`)

1. Create the secret (in a folder ignored by git, never committed). On Linux, the compose secret is
   mounted with the owner and permissions of the host file. The container runs as uid 10001, so that uid
   must own the file; otherwise the application cannot read it and keeps restarting.

   ```bash
   mkdir -p secrets && chmod 700 secrets
   printf '%s' 'your-long-password' > secrets/password.txt
   sudo chown 10001:10001 secrets/password.txt && sudo chmod 400 secrets/password.txt
   ```

   (The `secrets/` folder, with mode 700, belongs to your user. Docker can still traverse it, because it
   reads the file as root.)

   Also create `secrets/gmail_app_password.txt`: `docker compose` requires this file to exist, even if
   empty. An empty file disables receipt fetching; to enable it, see "Receipts".

   ```bash
   : > secrets/gmail_app_password.txt
   sudo chown 10001:10001 secrets/gmail_app_password.txt && sudo chmod 400 secrets/gmail_app_password.txt
   ```

2. Choose the listening address in a `.env` file next to the compose file (ignored by git; this way
   `docker compose stop/ps/down` also work in a new SSH session). `BIND_ADDR` is the Pi's WireGuard IP
   (for example `10.8.0.1`) or its LAN IP. It is required: `docker compose` refuses to start without it.
   Never use `0.0.0.0`.

   ```bash
   echo 'BIND_ADDR=10.8.0.1' > .env
   ```

   `IMAGE` (optional, defaults to `extranalyzer`, the locally built image) sets the image name; use
   `ghcr.io/<account>/<repository>` for the GitHub option. `IMAGE_TAG` (optional, defaults to `arm64`)
   selects another tag, for example `IMAGE_TAG=arm64-<commit>` to roll back to a specific version, or
   `IMAGE_TAG=amd64` to try it on an x86 PC.

3. Start:

   ```bash
   docker compose up -d
   docker compose ps          # the status should become "healthy"
   ```

4. Open `http://<BIND_ADDR>:8000` from a device connected to the WireGuard VPN.

The container is hardened: read-only filesystem (writes only to the `/data` volume and a 64 MB `/tmp`
tmpfs), `cap_drop: ALL`, `no-new-privileges`, 256 MB of memory, 1 CPU, capped logs (`max-size: 1m`,
`max-file: "3"`). Optional variable: `EXTRANALYZER_MAX_UPLOAD_MB` (defaults to 20).

### Starting at boot: waiting for WireGuard

> The current Pi does not use WireGuard but WolfNet (`10.10.10.95`, `wolfnet.service`). Its actual network
> setup is described in [docs/reseau.md](docs/reseau.md).

Docker cannot publish the port on the `wg0` IP until the interface exists. Recommended method: start the
Docker service after `wg-quick@wg0`.

```bash
sudo systemctl edit docker.service
```

then paste:

```ini
[Unit]
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
```

(Save, then run `sudo systemctl daemon-reload`.) With `restart: unless-stopped`, the container then
restarts on its own after a reboot, once `wg0` is up.

### Bank data retrieval (Enable Banking)

Optional: without it, the app works with PDF statements only. With it, account transactions arrive
automatically through open banking (PSD2, read-only), **after the latest PDF statement of each account**.
Design document: `docs/superpowers/specs/2026-11-12-enable-banking-design.md`.

1. **Import all PDF statements first**, up to the most recent one: banks often provide only 90 days of
   history, and a statement whose period already contains transactions from the bank is rejected.
2. Create an account on enablebanking.com, then a **Production** application in the Control Panel
   (`https://enablebanking.com/cp/applications`):
   - redirect URL (allowlist): `http://<BIND_ADDR>:8000/bank/callback`; if it is rejected, register for
     example `https://localhost/bank/callback` and use the copy-paste fallback (step 6);
   - the browser downloads the private key as `<identifier>.pem`;
   - "Activate by linking accounts": link your own Crédit Mutuel accounts (restricted mode, personal
     use). Check the displayed pricing terms at the same time.
3. Store the key as a secret, readable by the container's uid:

   ```bash
   cp <identifier>.pem secrets/enablebanking.pem
   sudo chown 10001:10001 secrets/enablebanking.pem && sudo chmod 400 secrets/enablebanking.pem
   ```

4. Complete `.env`:

   ```bash
   cat >> .env <<'EOF'
   COMPOSE_FILE=docker-compose.yml:docker-compose.banque.yml
   EB_APP_ID=<application identifier>
   EB_REDIRECT_URL=http://10.8.0.1:8000/bank/callback
   EOF
   docker compose up -d
   ```

   Optional: `SYNC_HOURS` (defaults to 12; 0 disables scheduled sync, the button remains) and `EB_ASPSP`
   (the bank preselected in the Enable Banking list, "Crédit Mutuel" by default). The container must be
   able to reach `https://api.enablebanking.com` outbound.
5. **Banque** (Bank) tab → "Connecter une banque" (Connect a bank): choose the country and bank from the
   Enable Banking list, authenticate on the bank's website, return to the app, link the accounts
   (automatic when the IBAN contains the statement account number) and run the first sync. Then check the
   balance check of each account.
   Several banks can be connected ("Ajouter une banque", Add a bank): each has its own consent, renewal
   and disconnection; scheduled sync goes through all of them, and a failure at one bank does not stop
   the others. In restricted mode, each new bank must also be linked in the Control Panel ("Link
   accounts"). Accounts from a bank with no PDF statement are created as "API only"; their type
   (checking or savings) is inferred from the bank and the account name, and can be corrected in the tab.
6. Fallback: if the return page does not open, copy the full address shown by the browser (with `code=`
   and `state=`) into "Le retour de la banque n'a pas abouti ?" (Bank return failed?) within 30 minutes.

Consent usually lasts 180 days; a "Banque : renouveler" (Bank: renew) badge appears 14 days before the
first bank's consent expires ("Renouveler le consentement", Renew consent, in the tab). The key never
leaves the Docker secret; no token, session identifier or full IBAN is ever logged or displayed.

### Solana wallets (Helius)

Optional: tracks, read-only, the euro value of Solana wallets (SOL, tokens, staking accounts) from their
public address. No private key or recovery phrase, and no writes to the blockchain. These values are not
mixed into the statement totals.

**Setup, from the interface (nothing to configure on the server):**

1. Create a free account on [dashboard.helius.dev](https://dashboard.helius.dev) and copy the key shown
   under "API Keys".
2. In Extranalyzer, **Banque** (Bank) tab, "Portefeuilles Solana" (Solana wallets) section: paste the key
   and click "Enregistrer la clé" (Save key). The application checks it with Helius before saving it.
3. Add your public addresses.

The key is stored in the data volume (`/data/helius.key`, mode 600), so it is part of the volume
backups. It is never written to the database, logged or displayed again; it can be replaced or deleted
from the same section. The container must be able to reach `https://mainnet.helius-rpc.com` outbound.
Scheduled refresh follows `SYNC_HOURS` (defaults to 12; 0 disables it).

**Administrator variant (optional)**: provide the key as a Docker secret. It then takes precedence over
the key entered in the interface, which can no longer change it.

```bash
read -rs HK && printf '%s' "$HK" > secrets/helius.key && unset HK
sudo chown 10001:10001 secrets/helius.key && sudo chmod 400 secrets/helius.key
# .env: add docker-compose.solana.yml to COMPOSE_FILE, then docker compose up -d
```

`HELIUS_URL` (optional) overrides the service address; it must start with `https://`.

### Troubleshooting

- Bank tab shows "non configurée" (not configured) even though `.env` is filled in: `docker compose logs`
  gives the cause (`clé privée illisible`, unreadable private key: redo the `chown`/`chmod` on
  `secrets/enablebanking.pem`).
- `lecture impossible de EXTRANALYZER_PASSWORD_FILE` (cannot read) in `docker compose logs`: the secret is
  not readable by uid 10001; redo the `chown 10001:10001` and `chmod 400` above, then
  `docker compose up -d --force-recreate`.
- `required variable BIND_ADDR is missing`: the `.env` file is missing or is not next to the compose file.
- `secrets/gmail_app_password.txt` not found on `docker compose up`: create the file (empty to disable
  fetching, see "Receipts").
- `lecture impossible de EXTRANALYZER_GMAIL_APP_PASSWORD_FILE` in `docker compose logs`: same cause as for
  the password; redo the `chown 10001:10001` and `chmod 400` on this file.

## Receipts

The application can fetch shopping receipts (PDF) received by email in Gmail, then match them with card
debits ("Courses" / Groceries tab). Fetching is disabled as long as the app password or `GMAIL_USER` is
missing.

1. On the Google account, enable 2-Step Verification, then create an app password (Google account,
   Security, App passwords). Google displays it in groups of 4 characters; the application removes the
   spaces, so you can paste it as is.
2. On the Pi, write it into the secret (the file already exists, owned by uid 10001 and read-only, so you
   need `sudo tee`), keeping the same permissions as the application password:

   ```bash
   printf '%s' 'xxxx xxxx xxxx xxxx' | sudo tee secrets/gmail_app_password.txt >/dev/null
   sudo chown 10001:10001 secrets/gmail_app_password.txt && sudo chmod 400 secrets/gmail_app_password.txt
   ```

3. Add the Gmail address to `.env` (ignored by git), then recreate the container:

   ```bash
   echo 'GMAIL_USER=your.address@gmail.com' >> .env
   docker compose up -d --force-recreate
   ```

What fetching does:

- Gmail is opened **read-only**: no email is marked as read, moved or deleted.
- It reads two Gmail labels, by default `Super U/Tickets de caisses` (store `superu`) and
  `intermarche/Tickets de caisses` (store `intermarche`), once an hour and at startup; the "Relever
  maintenant" (Fetch now) button in the Courses tab runs it on demand.
- PDFs are stored in `receipts/` in the `data` volume.
- The only outbound connection used for fetching is `imap.gmail.com:993` (verified TLS).
- Logs contain no address, password, product name or amount.

Optional variables (in `.env` next to the compose file): `RECEIPT_LABELS` (format
`label=store;label=store`, the two labels above by default) and, in the service,
`EXTRANALYZER_MAIL_INTERVAL_MIN` (defaults to 60, positive integer; add it to the compose file's
`environment:` to change it).

To disable fetching: empty the secret (`sudo sh -c ': > secrets/gmail_app_password.txt'`, permissions
unchanged) or leave `GMAIL_USER` empty (delete the line from `.env` or set it to `GMAIL_USER=`), then run
`docker compose up -d --force-recreate`. Receipts already fetched remain in the application.

## Backing up and restoring the `data` volume

The actual volume name is prefixed with the project name (`docker volume ls`, often `extranalyzer_data`).

```bash
docker compose stop
docker run --rm -v extranalyzer_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/extranalyzer-data-$(date +%F).tar.gz -C /data .
docker compose start
```

Restoring (stack stopped, volume created if missing, then restart):

```bash
docker compose down
docker volume create extranalyzer_data
docker run --rm -v extranalyzer_data:/data -v "$PWD":/backup alpine \
  sh -c "tar xzf /backup/extranalyzer-data-YYYY-MM-DD.tar.gz -C /data && chown -R 10001:10001 /data"
docker compose up -d
```

The archive contains the statement PDFs, the receipt PDFs (`receipts/`) and the database: store it
outside the repository, encrypted if it leaves the Pi.

## Updating

- **First update to the version with receipt fetching** (all options): the compose file now declares the
  `secrets/gmail_app_password.txt` secret, and `docker compose up -d` fails until it exists. Before
  updating, copy the new `docker-compose.yml` to the Pi (the published image does not contain it; with
  option B it comes with `git pull`), then create the secret (empty: fetching stays disabled, see
  "Receipts" to enable it):

  ```bash
  sudo sh -c ': > secrets/gmail_app_password.txt'
  sudo chown 10001:10001 secrets/gmail_app_password.txt && sudo chmod 400 secrets/gmail_app_password.txt
  ```

  On first startup, the database migrates to schema 8 (receipt tables) on its own; nothing else to do.
- **GitHub option**: push to `main`, wait for the workflow to finish (*Actions* tab), then on the Pi:
  `cd ~/extranalyzer && docker compose pull && docker compose up -d`.
- **Option A** (build on the PC): redo the `buildx`, `docker save` and `scp` above, then on the Pi:
  `gunzip -c extranalyzer-arm64.tar.gz | docker load && docker compose up -d`.
- **Option B** (build on the Pi): `git pull && docker build -t extranalyzer:arm64 . && docker compose up -d`.

`docker compose up -d` recreates the container; the `data` volume is kept.

## Local development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

## Pinned dependencies

`constraints.txt` pins the exact version of every package in the image: build tools (`setuptools`,
`wheel`), the dependencies from `pyproject.toml` and all their transitive dependencies. The `Dockerfile`
applies it (`-c constraints.txt`); `pyproject.toml` keeps unversioned dependencies for local development.
`tests/test_constraints.py` checks that nothing is missing and that the `Dockerfile` uses it.

Every version must exist as a wheel for linux/arm64 musl (`python:3.12-alpine`), otherwise the build
fails (`--only-binary=:all:`). To update:

1. Update the venv (`.venv/Scripts/python -m pip install -U -e ".[dev]"`) and make the tests pass.
2. Copy the installed versions (`.venv/Scripts/python -m pip freeze`) into `constraints.txt`, without
   `extranalyzer` or the development packages (`pytest`, `httpx2`, `reportlab`…).
3. Check, without Docker, that everything resolves to wheels for the image (both `musllinux` tags are
   needed: some packages, such as `pydantic_core`, only publish `musllinux_1_1`):

   ```bash
   .venv/Scripts/python -m pip install --dry-run --ignore-installed --target .tmp-check -c constraints.txt --only-binary=:all: --platform musllinux_1_2_aarch64 --platform musllinux_1_1_aarch64 --python-version 3.12 --implementation cp --abi cp312 setuptools wheel fastapi uvicorn python-multipart pdfplumber itsdangerous
   ```

   The "Would install" list must match the versions in `constraints.txt` exactly.
4. Rebuild the image ("Building and installing the image") and check that it becomes "healthy".

## Measured sizes

Measured with `docker image ls extranalyzer`:

| Platform     | Image size |
|--------------|------------|
| linux/arm64  | 122 MB     |
| linux/amd64  | 117 MB     |

Idle memory (container started, one statement imported): about 48 MB.
