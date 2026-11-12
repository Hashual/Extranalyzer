// Onglet « Banque » : connexion Enable Banking, comptes bancaires (rattachement, contrôle de solde), synchros.
import { el, emptyNote, formatDate, money, section } from "../ui.js";

export const id = "banque";
export const title = "Banque";

const LINK_STATUS = { active: "Connectée", expired: "Consentement expiré" };
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

function connectButton(ctx, label) {
  return action(label, async () => {
    const { url } = await ctx.api.post("/tabs/banque/connect");
    window.location.assign(url);
  }, ctx, { className: "primary" });
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

function linkCard(data, ctx) {
  const link = data.link;
  if (!link) {
    return section("Connexion",
      el("p", { text: `Aucune banque connectée. La connexion vous redirige vers ${data.aspsp_name} pour autoriser `
        + "la lecture de vos comptes." }),
      el("p", { className: "muted", text: "Importez d'abord tous vos relevés PDF jusqu'au plus récent : les opérations "
        + "de la banque ne sont reprises qu'après le dernier relevé de chaque compte." }),
      connectButton(ctx, "Connecter ma banque"));
  }
  const alert = data.alert && ALERT_TEXT[data.alert.status];
  const lines = [
    el("p", {}, el("span", { className: `status ${link.status === "active" ? "ok" : "error"}`,
      text: LINK_STATUS[link.status] || link.status }),
      ` ${link.aspsp_name} — consentement valable jusqu'au ${formatDate(link.valid_until.slice(0, 10))}`),
    alert ? el("p", { className: "error", attrs: { role: "status" }, text: alert(data.alert) }) : null,
    link.error ? el("p", { className: "muted", text: `Dernière erreur : ${link.error}` }) : null,
    link.retry_after ? el("p", { className: "muted",
      text: `Limite de la banque atteinte : prochaine synchro planifiée après ${dateTime(link.retry_after)}.` }) : null,
    el("p", { className: "muted", text: `Dernière synchronisation : ${dateTime(data.alert?.last_sync)}` }),
    el("div", { className: "filters" },
      link.status === "active"
        ? action("Synchroniser maintenant", async () => summaryText(await ctx.api.post("/tabs/banque/sync")), ctx,
          { className: "primary" })
        : null,
      connectButton(ctx, link.status === "active" ? "Renouveler le consentement" : "Reconnecter la banque"),
      action("Déconnecter", () => ctx.api.post("/tabs/banque/disconnect"), ctx,
        { confirm: "Déconnecter la banque ? Les opérations déjà importées sont conservées." })),
  ];
  return section("Connexion", ...lines);
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

function accountsCard(data, ctx) {
  if (!data.accounts.length) return section("Comptes", emptyNote("Aucun compte renvoyé par la banque."));
  const head = el("thead", {}, el("tr", {},
    ["Compte bancaire", "Rattaché à", "Contrôle de solde", "Dernière synchro"].map((h) =>
      el("th", { scope: "col", text: h }))));
  const body = el("tbody", {}, data.accounts.map((a) => el("tr", {},
    el("td", {}, `${a.name} ••${a.iban_tail}`, a.error ? el("p", { className: "error", text: a.error }) : null),
    el("td", {}, attachCell(a, data, ctx)),
    el("td", {}, balanceCell(a, ctx)),
    el("td", { text: dateTime(a.last_sync) }))));
  const unattached = data.accounts.some((a) => !a.account_id && !a.ignored);
  return section("Comptes",
    unattached ? el("p", { className: "muted", text: "Un compte « à rattacher » n'est pas synchronisé : au-delà de "
      + "90 jours, son historique peut être perdu si le choix tarde." }) : null,
    el("div", { className: "table-wrap scroll-x" }, el("table", { className: "data-table" }, head, body)));
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
  if (!data.configured && !data.link) {
    container.replaceChildren(notConfigured());
    return;
  }
  container.replaceChildren(...[
    notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
    data.configured ? null : notConfigured(),
    linkCard(data, ctx),
    data.link ? accountsCard(data, ctx) : null,
    runsCard(data),
    data.configured ? pasteCard(ctx) : null,
  ].filter(Boolean));
}
