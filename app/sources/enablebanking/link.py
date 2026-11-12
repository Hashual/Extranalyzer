"""Connexion à la banque, rattachement des comptes et synchronisation Enable Banking.

Les fonctions qui prennent `conn` ne valident pas (l'appelant possède la transaction), sauf `sync`, qui orchestre :
appels réseau d'abord, hors du verrou d'écriture, puis une seule transaction SQLite et une passe de classement.
Aucun jeton, identifiant de session ni IBAN n'est journalisé.
"""
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TypeVar

from app.config import BankSettings
from app.importer.amounts import to_cents
from app.importer.service import OnNewTransactions
from app.sources.base import AccountFetch
from app.sources.enablebanking import SOURCE
from app.sources.enablebanking.client import Client, EnableBankingError, RateLimited, SessionExpired
from app.sources.enablebanking.convert import convert_transaction, is_booked, pick_balance
from app.sources.service import balance_gap, frontier, store_fetch
from app.store.repo import fold

log = logging.getLogger(__name__)

STATE_TTL = timedelta(minutes=30)
EXPIRY_WARNING_DAYS = 14
RATE_LIMIT_PAUSE = timedelta(hours=6)
LOOKBACK_DAYS = 10
LONG_WINDOW_DAYS = 89
DEFAULT_CONSENT_S = 90 * 86400   # durée retenue si la banque n'annonce pas son maximum
WANTED_CONSENT_S = 180 * 86400   # durée souhaitée, plafonnée par le maximum de la banque
CONSENT_MARGIN_S = 3600          # marge sous le maximum : décalage d'horloge et latence (sinon refus de la banque)
MIN_NUMBER_DIGITS = 8        # un numéro de compte PDF plus court ne sert pas au rattachement automatique

T = TypeVar("T")
Runner = Callable[[Callable[[sqlite3.Connection], T]], T]


class StateError(ValueError):
    """Retour de la banque refusé : `state` inconnu, expiré ou déjà utilisé."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(moment: str) -> datetime:
    parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


# --- Connexion -------------------------------------------------------------------------------------------------

def start_connection(write: Runner, client: Client, bank: BankSettings, now: datetime) -> str:
    """Prépare l'autorisation et renvoie l'URL de la banque. Le `state` (usage unique) est enregistré dans une
    écriture courte et validée ; aucun appel réseau n'a lieu pendant une transaction."""
    wanted = fold(bank.aspsp_name)
    aspsp = next((a for a in client.aspsps(bank.country) if fold(str(a.get("name", ""))) == wanted), None)
    if aspsp is None:
        raise EnableBankingError(0, "ASPSP_NOT_FOUND",
                                 f"banque « {bank.aspsp_name} » absente de la liste d'Enable Banking ({bank.country})")
    seconds = consent_seconds(aspsp.get("maximum_consent_validity"))
    state = secrets.token_urlsafe(32)
    write(lambda conn: _save_state(conn, state, now))
    try:
        try:
            answer = client.start_auth(str(aspsp["name"]), bank.country, _iso(now + timedelta(seconds=seconds)),
                                       state, bank.redirect_url)
        except EnableBankingError as exc:
            # la banque annonce une limite plus courte que /aspsps : une seule nouvelle tentative sous cette limite
            stated = _stated_limit(exc)
            if stated is None or consent_seconds(stated) >= seconds:
                raise
            answer = client.start_auth(str(aspsp["name"]), bank.country,
                                       _iso(now + timedelta(seconds=consent_seconds(stated))), state, bank.redirect_url)
        url = answer.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise EnableBankingError(0, "INVALID_AUTH_URL",
                                     "adresse d'autorisation invalide renvoyée par Enable Banking")
    except BaseException:
        write(lambda conn: _drop_state(conn, state))
        raise
    return url


def consent_seconds(maximum) -> int:
    """Durée de consentement à demander : min(souhaitée, maximum annoncé par la banque) moins une marge.

    Demander exactement le maximum est refusé dès que l'horloge locale avance d'une seconde sur celle de la banque
    (WRONG_REQUEST_PARAMETERS). Un maximum absent ou illisible vaut 90 jours."""
    try:
        limit = int(float(maximum)) if not isinstance(maximum, bool) else 0
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        limit = DEFAULT_CONSENT_S
    return max(min(WANTED_CONSENT_S, limit) - CONSENT_MARGIN_S, 60)


def _stated_limit(exc: EnableBankingError) -> int | None:
    """Durée maximale (secondes) citée par un refus « consent validity more than N seconds »."""
    found = re.search(r"more than (\d+) seconds", exc.message)
    return int(found.group(1)) if found else None


def _save_state(conn: sqlite3.Connection, state: str, now: datetime) -> None:
    conn.execute("DELETE FROM bank_auth_states WHERE created_at < ?", (_iso(now - STATE_TTL),))
    conn.execute("INSERT INTO bank_auth_states(state, created_at) VALUES (?, ?)", (state, _iso(now)))
    conn.commit()


def _drop_state(conn: sqlite3.Connection, state: str) -> None:
    conn.execute("DELETE FROM bank_auth_states WHERE state = ?", (state,))
    conn.commit()


def consume_state(conn: sqlite3.Connection, state: str, now: datetime) -> None:
    """Vérifie et consomme le `state` d'un retour de la banque (usage unique, 30 minutes)."""
    row = conn.execute("SELECT created_at FROM bank_auth_states WHERE state = ?", (state,)).fetchone()
    conn.execute("DELETE FROM bank_auth_states WHERE state = ?", (state,))
    if row is None or _parse(row[0]) < now - STATE_TTL:
        raise StateError("retour de la banque refusé : demande inconnue, expirée ou déjà utilisée")


@dataclass
class Connected:
    link_id: int
    replaced_sessions: list[str] = field(default_factory=list)   # sessions à fermer (renouvellement)


def complete_connection(conn: sqlite3.Connection, client: Client, session: dict, now: datetime) -> Connected:
    """Enregistre la session ouverte par `POST /sessions` et ses comptes, puis les rattache."""
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise EnableBankingError(0, "INVALID_SESSION", "session invalide renvoyée par Enable Banking")
    aspsp = session.get("aspsp") if isinstance(session.get("aspsp"), dict) else {}
    access = session.get("access") if isinstance(session.get("access"), dict) else {}
    try:
        valid_until = _iso(_parse(str(access.get("valid_until"))))
    except ValueError:
        valid_until = _iso(now + timedelta(seconds=DEFAULT_CONSENT_S))
    replaced = [r[0] for r in conn.execute("SELECT session_id FROM bank_links WHERE status = 'active'")]
    conn.execute("UPDATE bank_links SET status = 'replaced' WHERE status = 'active'")
    link_id = conn.execute(
        "INSERT INTO bank_links(provider, session_id, aspsp_name, aspsp_country, valid_until, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (SOURCE, session_id, str(aspsp.get("name", "")), str(aspsp.get("country", "")), valid_until, _iso(now))
    ).lastrowid
    for account in session.get("accounts") or []:
        if isinstance(account, dict) and isinstance(account.get("uid"), str):
            _register_account(conn, link_id, account)
    return Connected(link_id, replaced)


def _register_account(conn: sqlite3.Connection, link_id: int, account: dict) -> None:
    ident = account.get("account_id") if isinstance(account.get("account_id"), dict) else {}
    iban = ident.get("iban") if isinstance(ident.get("iban"), str) else None
    id_hash = account.get("identification_hash") if isinstance(account.get("identification_hash"), str) else None
    name = account.get("name") or account.get("product") or account.get("details")
    values = (link_id, account["uid"], id_hash, iban, name if isinstance(name, str) else None,
              account.get("currency"), account.get("cash_account_type"))
    previous = None
    if id_hash:
        previous = conn.execute("SELECT id FROM external_accounts WHERE identification_hash = ? ORDER BY id DESC",
                                (id_hash,)).fetchone()
    if previous is None and iban:
        previous = conn.execute("SELECT id FROM external_accounts WHERE iban = ? ORDER BY id DESC", (iban,)).fetchone()
    if previous is not None:      # renouvellement : le rattachement et l'historique de synchro sont conservés
        conn.execute("UPDATE external_accounts SET link_id = ?, uid = ?, identification_hash = ?, iban = ?, name = ?, "
                     "currency = ?, cash_account_type = ?, last_error = NULL, last_sync_at = NULL WHERE id = ?",
                     (*values, previous[0]))     # last_sync_at NULL : historique long à la première synchro
        return
    external_id = conn.execute(
        "INSERT INTO external_accounts(link_id, uid, identification_hash, iban, name, currency, cash_account_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", values).lastrowid
    _auto_attach(conn, external_id)


def _candidates(conn: sqlite3.Connection, iban: str | None) -> list[int]:
    digits = _digits(iban)
    if not digits:
        return []
    return [account_id for account_id, number in conn.execute("SELECT id, number FROM accounts ORDER BY id")
            if len(_digits(number)) >= MIN_NUMBER_DIGITS and _digits(number) in digits]


def _auto_attach(conn: sqlite3.Connection, external_id: int) -> None:
    """Un seul compte connu dont le numéro figure dans l'IBAN : rattaché ; aucun : compte créé ; plusieurs :
    laissé « à rattacher »."""
    iban = conn.execute("SELECT iban FROM external_accounts WHERE id = ?", (external_id,)).fetchone()[0]
    found = _candidates(conn, iban)
    if len(found) == 1:
        conn.execute("UPDATE external_accounts SET account_id = ? WHERE id = ?", (found[0], external_id))
    elif not found:
        conn.execute("UPDATE external_accounts SET account_id = ? WHERE id = ?",
                     (_create_account(conn, external_id), external_id))


def _create_account(conn: sqlite3.Connection, external_id: int) -> int:
    uid, id_hash, iban, name, kind = conn.execute(
        "SELECT uid, identification_hash, iban, name, cash_account_type FROM external_accounts WHERE id = ?",
        (external_id,)).fetchone()
    number = iban or f"EB-{id_hash or uid}"
    existing = conn.execute("SELECT id FROM accounts WHERE number = ?", (number,)).fetchone()
    if existing is not None:
        return existing[0]
    return conn.execute("INSERT INTO accounts(number, label, kind, source) VALUES (?, ?, ?, ?)",
                        (number, name or "Compte bancaire", "savings" if kind == "SVGS" else "checking",
                         SOURCE)).lastrowid


def attach(conn: sqlite3.Connection, external_id: int, account_id: int | None, *, ignore: bool = False) -> None:
    """Rattache un compte bancaire à un compte connu (`account_id`), à un nouveau compte (`None`), ou l'ignore.

    Un compte qui a déjà importé des opérations ne change plus de compte (il peut seulement être ignoré ou repris).
    """
    row = conn.execute("SELECT account_id FROM external_accounts WHERE id = ?", (external_id,)).fetchone()
    if row is None:
        raise LookupError(f"compte bancaire {external_id} introuvable")
    current = row[0]
    if ignore:
        conn.execute("UPDATE external_accounts SET ignored = 1 WHERE id = ?", (external_id,))
        return
    if account_id is not None and conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise LookupError(f"compte {account_id} introuvable")
    if current is not None and account_id != current and _imported(conn, current):
        raise ValueError("ce compte bancaire a déjà importé des opérations : son rattachement ne change plus")
    if account_id is None:
        account_id = current if current is not None and _imported(conn, current) else _create_account(conn, external_id)
    conn.execute("UPDATE external_accounts SET account_id = ?, ignored = 0 WHERE id = ?", (account_id, external_id))


def _imported(conn: sqlite3.Connection, account_id: int) -> bool:
    return conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND source = ? LIMIT 1",
                        (account_id, SOURCE)).fetchone() is not None


def accept_gap(conn: sqlite3.Connection, external_id: int) -> None:
    """Mémorise l'écart de solde actuel comme expliqué : il est retranché des contrôles suivants."""
    cur = conn.execute("UPDATE external_accounts SET accepted_gap_cents = accepted_gap_cents + balance_gap_cents, "
                       "balance_gap_cents = 0 WHERE id = ? AND balance_gap_cents IS NOT NULL", (external_id,))
    if cur.rowcount == 0:
        raise LookupError(f"aucun écart à accepter pour le compte bancaire {external_id}")


def revoke(conn: sqlite3.Connection) -> list[str]:
    """Déconnecte la banque : liens actifs ou expirés passés `revoked` ; renvoie les sessions à fermer.
    Les opérations importées restent."""
    sessions = [r[0] for r in conn.execute("SELECT session_id FROM bank_links WHERE status IN ('active', 'expired')")]
    conn.execute("UPDATE bank_links SET status = 'revoked' WHERE status IN ('active', 'expired')")
    return sessions


# --- Synchronisation ------------------------------------------------------------------------------------------

@dataclass
class Fetched:
    external_id: int
    link_id: int
    fetch: AccountFetch | None
    error: EnableBankingError | None = None


@dataclass
class SyncSummary:
    run_id: int
    added: int
    skipped: int
    errors: int
    message: str | None


def _date_from(conn: sqlite3.Connection, account_id: int) -> date | None:
    limit = frontier(conn, account_id)
    last = conn.execute("SELECT MAX(op_date) FROM transactions WHERE account_id = ? AND source = ?",
                        (account_id, SOURCE)).fetchone()[0]
    starts = [d for d in (limit and limit + timedelta(days=1),
                          last and date.fromisoformat(last) - timedelta(days=LOOKBACK_DAYS)) if d is not None]
    return max(starts) if starts else None


def fetch_all(conn: sqlite3.Connection, client: Client, now: datetime) -> list[Fetched]:
    """Récupère les comptes rattachés des liens actifs (lecture seule de la base, appels réseau).

    Une session expirée ou une limite de la banque arrête les autres comptes du même lien."""
    rows = conn.execute(
        "SELECT e.id, e.link_id, e.uid, e.account_id, e.last_sync_at FROM external_accounts e "
        "JOIN bank_links l ON l.id = e.link_id WHERE l.status = 'active' AND e.account_id IS NOT NULL "
        "AND e.ignored = 0 AND (l.retry_after IS NULL OR l.retry_after <= ?) ORDER BY e.id", (_iso(now),)).fetchall()
    results: list[Fetched] = []
    stopped: set[int] = set()
    for external_id, link_id, uid, account_id, last_sync in rows:
        if link_id in stopped:
            continue
        start = _date_from(conn, account_id)
        # « longest » : première synchro d'un lien, ou fenêtre plus ancienne que ce que la banque garde (~90 jours)
        old = start is None or start < now.date() - timedelta(days=LONG_WINDOW_DAYS)
        try:
            raw = client.transactions(uid, start, "longest" if last_sync is None or old else "default")
            balance = pick_balance(client.balances(uid))
        except EnableBankingError as exc:
            results.append(Fetched(external_id, link_id, None, exc))
            if isinstance(exc, (SessionExpired, RateLimited)):
                stopped.add(link_id)
            continue
        booked = [t for t in raw if is_booked(t)]
        converted = [convert_transaction(t) for t in booked]
        kept = [t for t in converted if t is not None]
        results.append(Fetched(external_id, link_id,
                               AccountFetch(account_id, kept, balance, len(converted) - len(kept), None, external_id)))
    return results


def write_sync(conn: sqlite3.Connection, results: list[Fetched], trigger: str, on_new: OnNewTransactions,
               started: datetime, now: datetime) -> SyncSummary:
    """Écrit le résultat d'une récupération : opérations, soldes, erreurs, journal ; une passe de classement."""
    pending: list[int] = []
    added = skipped = errors = 0
    messages: list[str] = []
    failed_links: set[int] = set()
    for r in results:
        if r.error is not None:
            errors += 1
            failed_links.add(r.link_id)
            messages.append(r.error.message)
            if isinstance(r.error, SessionExpired):     # un lien remplacé entre-temps n'est pas touché
                conn.execute("UPDATE bank_links SET status = 'expired', last_error = ? WHERE id = ? "
                             "AND status = 'active'", (r.error.message, r.link_id))
            elif isinstance(r.error, RateLimited):
                conn.execute("UPDATE bank_links SET retry_after = ?, last_error = ? WHERE id = ? AND status = 'active'",
                             (_iso(now + RATE_LIMIT_PAUSE), r.error.message, r.link_id))
            conn.execute("UPDATE external_accounts SET last_error = ? WHERE id = ?", (r.error.message, r.external_id))
            continue
        ids, before = store_fetch(conn, SOURCE, r.fetch)
        pending += ids
        added, skipped, errors = added + len(ids), skipped + before, errors + r.fetch.rejected
        if r.fetch.rejected:
            messages.append(f"{r.fetch.rejected} opération(s) inutilisable(s) écartée(s)")
        gap = balance_gap(conn, r.fetch.account_id, r.fetch.balance)
        accepted = conn.execute("SELECT accepted_gap_cents FROM external_accounts WHERE id = ?",
                                (r.external_id,)).fetchone()[0]
        balance = r.fetch.balance
        conn.execute("UPDATE external_accounts SET last_sync_at = ?, balance_cents = ?, balance_date = ?, "
                     "balance_gap_cents = ?, last_error = NULL WHERE id = ?",
                     (_iso(now), None if balance is None else to_cents(balance.amount),
                      None if balance is None else balance.reference_date.isoformat(),
                      None if gap is None else gap - accepted, r.external_id))
    for link_id in {r.link_id for r in results} - failed_links:
        conn.execute("UPDATE bank_links SET last_error = NULL, retry_after = NULL WHERE id = ?", (link_id,))
    paused = conn.execute("SELECT MIN(retry_after) FROM bank_links WHERE status = 'active' AND retry_after > ?",
                          (_iso(now),)).fetchone()[0]
    if paused is not None:
        messages.append(f"limite de consultations de la banque : pas de nouvelle tentative avant {paused}")
    if pending:
        on_new(conn, pending)
    message = " ; ".join(dict.fromkeys(messages))[:500] or None
    run_id = conn.execute("INSERT INTO sync_runs(started_at, finished_at, trigger, added, skipped, errors, message) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (_iso(started), _iso(now), trigger, added, skipped, errors, message)).lastrowid
    return SyncSummary(run_id, added, skipped, errors, message)


def sync(read: Runner, write: Runner, client: Client, trigger: str, on_new: OnNewTransactions,
         clock: Callable[[], datetime]) -> SyncSummary:
    """Une synchronisation complète : récupération (sans verrou d'écriture) puis une transaction d'écriture."""
    started = clock()
    results = read(lambda conn: fetch_all(conn, client, started))

    def run(conn: sqlite3.Connection) -> SyncSummary:
        conn.execute("BEGIN")
        try:
            summary = write_sync(conn, results, trigger, on_new, started, clock())
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return summary

    summary = write(run)
    log.info("synchro bancaire (%s) : %d ajoutée(s), %d avant la frontière, %d erreur(s)",
             trigger, summary.added, summary.skipped, summary.errors)
    return summary


# --- État pour l'interface ---------------------------------------------------------------------------------

def bank_status(conn: sqlite3.Connection, now: datetime) -> dict | None:
    """Alerte de la barre commune : None sans connexion, sinon statut, jours restants, dernière synchro."""
    link = conn.execute("SELECT id, status, valid_until, last_error FROM bank_links "
                        "WHERE status IN ('active', 'expired') ORDER BY id DESC LIMIT 1").fetchone()
    if link is None:
        return None
    link_id, status, valid_until, link_error = link
    days_left = (_parse(valid_until) - now).days
    account_error = conn.execute("SELECT 1 FROM external_accounts WHERE link_id = ? AND ignored = 0 "
                                 "AND last_error IS NOT NULL LIMIT 1", (link_id,)).fetchone()
    to_attach = conn.execute("SELECT COUNT(*) FROM external_accounts WHERE link_id = ? AND ignored = 0 "
                             "AND account_id IS NULL", (link_id,)).fetchone()[0]
    if status == "expired" or _parse(valid_until) <= now:
        state = "expired"
    elif days_left <= EXPIRY_WARNING_DAYS:
        state = "expiring"
    elif link_error or account_error:
        state = "error"
    else:
        state = "ok"
    last_sync = conn.execute("SELECT MAX(finished_at) FROM sync_runs").fetchone()[0]
    return {"status": state, "days_left": max(days_left, 0), "valid_until": valid_until, "last_sync": last_sync,
            "to_attach": to_attach}
