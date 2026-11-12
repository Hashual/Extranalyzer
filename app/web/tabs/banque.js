// Onglet « Banque » : connexions Enable Banking (une par banque), comptes bancaires (rattachement, type, contrôle
// de solde), ajout d'une banque choisie dans la liste d'Enable Banking, synchros.
import { el, emptyNote, formatDate, money, section } from "../ui.js";

export const id = "banque";
export const title = "Banque";

const LINK_STATUS = { active: "Connectée", expired: "Consentement expiré" };
const KIND_LABELS = { checking: "Courant", savings: "Épargne", exchange: "Change", wallet: "Portefeuille" };
// Pays couverts par Enable Banking les plus probables ; FR par défaut
const COUNTRIES = [["FR", "France"], ["BE", "Belgique"], ["LU", "Luxembourg"], ["DE", "Allemagne"],
  ["ES", "Espagne"], ["IT", "Italie"], ["NL", "Pays-Bas"], ["PT", "Portugal"], ["IE", "Irlande"],
  ["AT", "Autriche"], ["FI", "Finlande"], ["SE", "Suède"], ["DK", "Danemark"], ["NO", "Norvège"],
  ["PL", "Pologne"], ["EE", "Estonie"], ["LV", "Lettonie"], ["LT", "Lituanie"]];
const ALERT_TEXT = {
  ok: "",
  expiring: (a) => `Le consentement expire dans ${a.days_left} jour(s) : renouvelez la connexion.`,
  expired: () => "Le consentement a expiré : reconnectez la banque pour reprendre la synchronisation.",
  error: () => "La dernière synchronisation a rencontré une erreur (détail ci-dessous).",
};

let lastNotice = null;      // résultat d'une action, affiché une fois après le rafraîchissement

function dateTime(iso) {
  if (!iso) return "jamais";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

function summaryText(s) {
  const parts = [`${s.added} opération(s) ajoutée(s)`];
  if (s.skipped) parts.push(`${s.skipped} déjà couverte(s) par les relevés PDF`);
  if (s.errors) parts.push(`${s.errors} erreur(s)`);
  return parts.join(", ") + (s.message ? ` — ${s.message}` : "");
}

/** Bouton qui lance une action, affiche son erreur et rafraîchit l'appli. */
function action(label, run, ctx, { className = "", confirm = null } = {}) {
  const button = el("button", { type: "button", className, text: label });
  const error = el("span", { className: "error", attrs: { role: "alert" } });
  button.addEventListener("click", async () => {
    if (confirm && !window.confirm(confirm)) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const notice = await run();
      if (typeof notice === "string") lastNotice = notice;
      await ctx.refresh();
    } catch (e) {
      error.textContent = e.message;
      button.disabled = false;
    }
  });
  return el("span", { className: "row-actions" }, button, error);
}

async function goToBank(ctx, aspspName, country) {
  const { url } = await ctx.api.post("/tabs/banque/connect", { aspsp_name: aspspName, country });
  window.location.assign(url);
}

function connectButton(ctx, label, link) {
  return action(label, () => goToBank(ctx, link.aspsp_name, link.country), ctx, { className: "primary" });
}

/** Normalisation de recherche : minuscules, sans accents. */
function fold(text) {
  return text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function notConfigured() {
  return section("Récupération bancaire non configurée",
    el("p", { text: "Pour récupérer automatiquement vos opérations, fournissez au conteneur :" }),
    el("ul", { className: "plain-list" },
      el("li", { text: "EXTRANALYZER_EB_APP_ID : identifiant de l'application Enable Banking ;" }),
      el("li", { text: "la clé privée de l'application en secret Docker (enablebanking_key) ;" }),
      el("li", { text: "EXTRANALYZER_EB_REDIRECT_URL : l'adresse de retour déclarée dans le Control Panel." })),
    el("p", { className: "muted", text: "Marche à suivre détaillée : README, section « Récupération bancaire »." }));
}

function linkCard(link, data, ctx) {
  const state = link.state;
  const alert = state && ALERT_TEXT[state.status];
  return section(link.aspsp_name,
    el("p", {}, el("span", { className: `status ${link.status === "active" ? "ok" : "error"}`,
      text: LINK_STATUS[link.status] || link.status }),
      ` — consentement valable jusqu'au ${formatDate(link.valid_until.slice(0, 10))}`),
    alert ? el("p", { className: "error", attrs: { role: "status" }, text: alert(state) }) : null,
    link.error ? el("p", { className: "muted", text: `Dernière erreur : ${link.error}` }) : null,
    link.retry_after ? el("p", { className: "muted",
      text: `Limite de la banque atteinte : prochaine synchro planifiée après ${dateTime(link.retry_after)}.` }) : null,
    el("p", { className: "muted", text: `Dernière synchronisation : ${dateTime(state?.last_sync)}` }),
    el("div", { className: "filters" },
      link.status === "active"
        ? action("Synchroniser", async () => summaryText(await ctx.api.post("/tabs/banque/sync",
          { link_id: link.id })), ctx, { className: "primary" })
        : null,
      connectButton(ctx, link.status === "active" ? "Renouveler le consentement" : "Reconnecter", link),
      action("Déconnecter", () => ctx.api.post(`/tabs/banque/links/${link.id}/disconnect`), ctx,
        { confirm: `Déconnecter ${link.aspsp_name} ? Les opérations déjà importées sont conservées.` })),
    accountsTable(link, data, ctx));
}

/** Ajout d'une banque : pays, recherche par nom dans la liste d'Enable Banking, puis autorisation. */
function addBankCard(data, ctx) {
  const hasLinks = data.links.length > 0;
  const country = el("select", { attrs: { "aria-label": "Pays de la banque" } },
    COUNTRIES.map(([code, name]) => el("option", { value: code, text: `${name} (${code})` })));
  country.value = COUNTRIES.some(([code]) => code === data.country) ? data.country : "FR";
  const search = el("input", { type: "search", placeholder: "Rechercher une banque…",
    attrs: { "aria-label": "Rechercher une banque" } });
  const list = el("select", { size: 8, className: "bank-list", attrs: { "aria-label": "Banques proposées" } });
  const info = el("p", { className: "muted", attrs: { role: "status" } });
  const connected = new Set(data.links.map((l) => `${fold(l.aspsp_name)}|${l.country}`));
  let banks = [];

  function fill() {
    const wanted = fold(search.value.trim());
    const shown = banks.filter((b) => fold(b.name).includes(wanted));
    list.replaceChildren(...shown.slice(0, 300).map((b) => el("option", { value: b.name,
      text: connected.has(`${fold(b.name)}|${b.country}`) ? `${b.name} (déjà connectée)` : b.name })));
    const preferred = shown.find((b) => fold(b.name) === fold(data.aspsp_name || ""));
    if (preferred && !wanted) list.value = preferred.name;
    else if (shown.length === 1) list.value = shown[0].name;
    info.textContent = banks.length ? `${shown.length} banque(s) sur ${banks.length}` : "";
  }

  async function load() {
    info.textContent = "Chargement de la liste des banques…";
    list.replaceChildren();
    try {
      banks = (await ctx.api.get("/tabs/banque/aspsps", { country: country.value })).aspsps;
      fill();
    } catch (e) {
      banks = [];
      info.textContent = e.message;
    }
  }

  country.addEventListener("change", load);
  search.addEventListener("input", fill);
  const connect = action("Connecter cette banque", async () => {
    if (!list.value) throw new Error("choisissez une banque dans la liste");
    await goToBank(ctx, list.value, country.value);
  }, ctx, { className: "primary" });
  const details = el("details", { className: "card", open: !hasLinks },
    el("summary", { text: hasLinks ? "Ajouter une banque" : "Connecter une banque" }),
    el("p", { text: "Choisissez votre banque : la connexion vous redirige vers elle pour autoriser la lecture de "
      + "vos comptes (lecture seule)." }),
    el("p", { className: "muted", text: "Application Enable Banking en mode restreint : liez aussi cette banque "
      + "dans le Control Panel d'Enable Banking (« Link accounts ») avant de vous connecter." }),
    el("p", { className: "muted", text: "Si vous avez des relevés PDF de cette banque, importez-les d'abord jusqu'au "
      + "plus récent. Sans relevé, ses comptes sont créés automatiquement (« API seulement »)." }),
    el("div", { className: "filters" }, country, search),
    list, info, connect);
  let loaded = false;
  const ensure = () => {
    if (details.open && !loaded) {
      loaded = true;
      load();
    }
  };
  details.addEventListener("toggle", ensure);
  queueMicrotask(ensure);
  return details;
}

function balanceCell(a, ctx) {
  if (a.gap === null || a.gap === undefined) {
    return el("span", { className: "muted", text: a.balance === null ? "contrôle impossible"
      : `solde ${money(a.balance)} (contrôle impossible)` });
  }
  if (Number(a.gap) === 0) {
    return el("span", { className: "status ok", text: `solde vérifié${a.balance_date ? " au " + formatDate(a.balance_date) : ""}` });
  }
  return el("span", {},
    el("span", { className: "status error", text: `écart de ${money(a.gap)} au ${formatDate(a.balance_date)}` }),
    action("Accepter l'écart", () => ctx.api.post(`/tabs/banque/accounts/${a.id}/accept-gap`), ctx,
      { confirm: "Cet écart est expliqué (trou d'historique, opération écartée…) ? Il ne sera plus signalé." }));
}

function attachCell(a, data, ctx) {
  if (a.ignored) {
    return el("span", {}, el("span", { className: "muted", text: "ignoré " }),
      action("Reprendre", () => ctx.api.post(`/tabs/banque/accounts/${a.id}/attach`,
        { account_id: a.account_id, ignore: false }), ctx));
  }
  const current = a.account_id
    ? el("span", { text: `${a.account_label} ••${a.account_tail}` })
    : el("span", { className: "status conflict", text: "à rattacher" });
  const nodes = [current];
  if (!a.locked) {
    const select = el("select", { attrs: { "aria-label": "Rattacher à" } },
      el("option", { value: "", text: a.account_id ? "Changer…" : "Choisir…" }),
      data.known_accounts.map((k) => el("option", { value: String(k.id), text: `${k.label} ••${k.number_tail}` })),
      el("option", { value: "new", text: "Nouveau compte" }));
    const error = el("span", { className: "error", attrs: { role: "alert" } });
    select.addEventListener("change", async () => {
      if (!select.value) return;
      try {
        await ctx.api.post(`/tabs/banque/accounts/${a.id}/attach`,
          { account_id: select.value === "new" ? null : Number(select.value), ignore: false });
        await ctx.refresh();
      } catch (e) {
        error.textContent = e.message;
      }
    });
    nodes.push(select, error);
  }
  nodes.push(action("Ignorer", () => ctx.api.post(`/tabs/banque/accounts/${a.id}/attach`, { ignore: true }), ctx,
    { className: "link" }));
  return el("span", { className: "row-actions" }, ...nodes);
}

/** Type du compte : modifiable (courant ou épargne) pour un compte « API seulement », sinon celui des relevés. */
function kindCell(a, ctx) {
  if (!a.account_id) return el("span", { className: "muted", text: "—" });
  if (!a.api_only) return el("span", { text: KIND_LABELS[a.account_kind] || a.account_kind });
  const select = el("select", { attrs: { "aria-label": "Type du compte" } },
    el("option", { value: "checking", text: "Courant" }), el("option", { value: "savings", text: "Épargne" }));
  select.value = a.account_kind;
  const error = el("span", { className: "error", attrs: { role: "alert" } });
  select.addEventListener("change", async () => {
    try {
      await ctx.api.post(`/tabs/banque/accounts/${a.id}/kind`, { kind: select.value });
      await ctx.refresh();
    } catch (e) {
      error.textContent = e.message;
    }
  });
  return el("span", { className: "row-actions" }, select, el("span", { className: "muted", text: "API seulement" }),
    error);
}

function accountsTable(link, data, ctx) {
  if (!link.accounts.length) return emptyNote("Aucun compte renvoyé par la banque.");
  const head = el("thead", {}, el("tr", {},
    ["Compte bancaire", "Rattaché à", "Type", "Contrôle de solde", "Dernière synchro"].map((h) =>
      el("th", { scope: "col", text: h }))));
  const body = el("tbody", {}, link.accounts.map((a) => el("tr", {},
    el("td", {}, `${a.name} ••${a.iban_tail}`, a.error ? el("p", { className: "error", text: a.error }) : null),
    el("td", {}, attachCell(a, data, ctx)),
    el("td", {}, kindCell(a, ctx)),
    el("td", {}, balanceCell(a, ctx)),
    el("td", { text: dateTime(a.last_sync) }))));
  const unattached = link.accounts.some((a) => !a.account_id && !a.ignored);
  return el("div", {},
    unattached ? el("p", { className: "muted", text: "Un compte « à rattacher » n'est pas synchronisé : au-delà de "
      + "90 jours, son historique peut être perdu si le choix tarde." }) : null,
    el("div", { className: "table-wrap scroll-x" }, el("table", { className: "data-table" }, head, body)));
}

function globalActions(data, ctx) {
  const active = data.links.filter((l) => l.status === "active");
  if (active.length < 2) return null;
  return el("div", { className: "filters" },
    action("Synchroniser toutes les banques", async () => summaryText(await ctx.api.post("/tabs/banque/sync")), ctx,
      { className: "primary" }));
}

function runsCard(data) {
  if (!data.runs.length) return null;
  return section("Synchronisations récentes",
    el("ul", { className: "plain-list" }, data.runs.map((r) =>
      el("li", { text: `${dateTime(r.finished_at || r.started_at)} (${r.trigger}) : ${summaryText(r)}` }))));
}

function pasteCard(ctx) {
  const input = el("input", { type: "url", placeholder: "Adresse affichée après l'autorisation à la banque",
    attrs: { "aria-label": "Adresse de retour de la banque" } });
  const form = el("div", { className: "filters" }, input,
    action("Valider", async () => {
      if (!input.value.trim()) throw new Error("collez l'adresse complète de retour");
      return summaryText(await ctx.api.post("/tabs/banque/session", { url: input.value }));
    }, ctx));
  return el("details", { className: "card" },
    el("summary", { text: "Le retour de la banque n'a pas abouti ?" }),
    el("p", { className: "muted", text: "Si l'adresse de retour n'est pas joignable, copiez l'adresse complète de la page "
      + "d'arrivée (elle contient code= et state=) et collez-la ici, dans les 30 minutes." }),
    form);
}

export async function render(container, ctx) {
  const data = await ctx.api.get("/tabs/banque/status");
  const notice = lastNotice;
  lastNotice = null;
  if (!data.configured && !data.links.length) {
    container.replaceChildren(notConfigured());
    return;
  }
  container.replaceChildren(...[
    notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
    data.configured ? null : notConfigured(),
    globalActions(data, ctx),
    ...data.links.map((link) => linkCard(link, data, ctx)),
    data.configured ? addBankCard(data, ctx) : null,
    runsCard(data),
    data.configured ? pasteCard(ctx) : null,
  ].filter(Boolean));
}
