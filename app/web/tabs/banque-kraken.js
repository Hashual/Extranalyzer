// Section « Compte Kraken » de l'onglet Connexions : clé API en lecture seule collée ici, avoirs lus dans Épargne.
// Les montants en euros passent par money(…, "balance") : ils suivent le masquage des soldes.
import { confirmDialog, el, hasTab, money, section } from "../ui.js";

const KRAKEN_API = "https://www.kraken.com/u/security/api";

let lastNotice = null;      // résultat d'une action, affiché une fois après le rafraîchissement

function dateTime(iso) {
  if (!iso) return "jamais";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

/** Bouton qui lance une action, affiche son erreur et rafraîchit l'appli. */
function action(label, run, ctx, { className = "", confirm = null } = {}) {
  const button = el("button", { type: "button", className, text: label });
  const error = el("span", { className: "error", attrs: { role: "alert" } });
  button.addEventListener("click", async () => {
    if (confirm && !(await confirmDialog(confirm))) return;
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

function refreshNotice(r) {
  if (!r) return "lecture en attente : cliquez sur Actualiser.";
  return r.errors ? "la lecture des soldes a échoué (voir ci-dessous)." : "soldes lus.";
}

/** Clé API et clé privée : vérifiées par le serveur auprès de Kraken avant d'être enregistrées. */
function keyForm(ctx, label) {
  const input = (placeholder) => el("input", { type: "password", placeholder,
    attrs: { "aria-label": placeholder, autocomplete: "off", spellcheck: "false" } });
  const key = input("Clé API Kraken");
  const secret = input("Clé privée Kraken");
  const save = action(label, async () => {
    if (!key.value.trim() || !secret.value.trim()) throw new Error("collez la clé API et la clé privée");
    const r = await ctx.api.put("/tabs/banque/kraken/key", { api_key: key.value.trim(), api_secret: secret.value.trim() });
    key.value = "";
    secret.value = "";
    return `Compte Kraken relié : ${refreshNotice(r.refresh)}`;
  }, ctx, { className: "primary" });
  return el("div", { className: "filters" }, key, secret, save);
}

function howTo(ctx) {
  return section("Compte Kraken : relier en lecture seule",
    el("p", { text: "L'application lit vos soldes Kraken et les affiche en euros dans l'onglet Épargne. Il lui faut "
      + "une clé API en lecture seule :" }),
    el("ol", {},
      el("li", {}, "sur Kraken, ouvrez ", el("a", { href: KRAKEN_API, text: "Paramètres → Sécurité → API",
        attrs: { target: "_blank", rel: "noreferrer" } }), " et créez une clé ;"),
      el("li", {}, "cochez ", el("strong", { text: "uniquement « Query Funds »" }), " (consulter les fonds) : ni "
        + "trading, ni dépôt, ni retrait ;"),
      el("li", { text: "copiez la « clé API » et la « clé privée » affichées, puis collez-les ici." })),
    keyForm(ctx, "Relier Kraken"),
    el("p", { className: "muted", text: "La clé est vérifiée auprès de Kraken puis gardée sur votre serveur ; elle "
      + "n'est jamais réaffichée. Avec la seule permission « Query Funds », elle ne peut rien acheter, vendre ni retirer." }));
}

function accountLine(account) {
  if (!account) return null;
  return el("p", {}, el("strong", { text: account.label }), " · ",
    account.total_eur === null ? "aucun relevé" : money(account.total_eur, "balance"),
    el("span", { className: "muted", text: ` · dernier relevé : ${dateTime(account.last_sync_at)}` }),
    account.last_error
      ? el("span", { className: "error", attrs: { role: "alert" }, text: ` · échec : ${account.last_error}` }) : null);
}

function linked(account, notice, ctx) {
  return el("section", { className: "card" },
    el("div", { className: "wallets-head" },
      el("div", {}, el("h2", { text: "Compte Kraken" }),
        el("p", { className: "muted", text: "Relié en lecture seule (soldes valorisés en euros)." })),
      action("Actualiser", async () => {
        const r = await ctx.api.post("/tabs/banque/kraken/refresh", {});
        return r.errors ? "Actualisation en échec." : "Soldes Kraken actualisés.";
      }, ctx, { className: "primary" })),
    notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
    accountLine(account),
    account && hasTab(ctx, "epargne")
      ? el("p", {}, el("button", { type: "button", className: "link",
        text: "Avoirs et évolution : voir l'onglet Épargne", on: { click: () => ctx.navigate("epargne") } }))
      : null,
    el("details", {}, el("summary", { text: "Clé Kraken" }),
      el("p", { className: "muted", text: "Une clé est enregistrée. Pour la changer, collez la nouvelle :" }),
      keyForm(ctx, "Remplacer la clé"),
      action("Délier Kraken", () => ctx.api.delete("/tabs/banque/kraken/key"), ctx, { className: "danger",
        confirm: "Délier Kraken ? La clé est supprimée ; le compte et son historique restent dans Épargne, sans être "
          + "actualisés. Pensez aussi à supprimer la clé dans Kraken." })));
}

export async function krakenSection(ctx) {
  const [status, wallets] = await Promise.all([ctx.api.get("/tabs/banque/kraken"), ctx.api.get("/tabs/banque/wallets")]);
  const account = wallets.wallets.find((w) => w.provider === "kraken") || null;
  const notice = lastNotice;
  lastNotice = null;
  if (status.configured) return linked(account, notice, ctx);
  return el("div", {}, notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
    howTo(ctx),
    account ? el("p", { className: "muted", text: `Le compte « ${account.label} » et son historique sont conservés, `
      + "mais ne sont plus actualisés sans clé." }) : null);
}
