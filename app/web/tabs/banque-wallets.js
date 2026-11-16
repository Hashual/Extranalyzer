// Section « Portefeuilles Solana » de l'onglet Connexions : suivi en lecture seule (SOL, jetons, staking) valorisé en euros.
// Tous les montants en euros passent par money(…, "balance") : ils suivent le masquage des soldes.
import { el, emptyNote, hasTab, money, section } from "../ui.js";
import { isMasked, MASK } from "../privacy.js";

const SOLSCAN = "https://solscan.io/account/";
const STAKE_STATES = { activating: "en activation", active: "actif", deactivating: "en désactivation",
  inactive: "inactif" };
const QUANTITY = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 4 });
const PRICE = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 6 });
const PRICE_2 = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const formatPrice = (price) => (Math.abs(price) >= 1 ? PRICE_2 : PRICE).format(price);   // 6 décimales : jetons à quelques centimes

let lastNotice = null;      // résultat d'une action, affiché une fois après le rafraîchissement

function short(text) {
  return text && text.length > 10 ? `${text.slice(0, 4)}…${text.slice(-4)}` : text || "";
}

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
    if (confirm && !window.confirm(confirm)) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const notice = await run();
      if (notice === false) {         // action abandonnée (invite annulée) : rien à rafraîchir
        button.disabled = false;
        return;
      }
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
  if (r.errors) return `${r.refreshed} portefeuille(s) actualisé(s), ${r.errors} en erreur.`;
  return `${r.refreshed} portefeuille(s) actualisé(s).`;
}

function head(labels) {
  return el("thead", {}, el("tr", {}, labels.map((h, i) =>
    el("th", { scope: "col", className: i > 0 && i < 4 ? "num" : "", text: h }))));
}

function row(first, quantity, price, value, last) {
  return el("tr", {},
    el("td", {}, first),
    el("td", { className: "num", text: quantity }),
    el("td", { className: "num", text: price }),
    el("td", { className: "num", text: value }),
    el("td", {}, last));
}

// Quantité et prix sont masqués avec les soldes : valeur = quantité × prix s'en déduirait sinon.
const priceText = (p) => (isMasked("balance") ? MASK : p.price_eur === null ? "—" : formatPrice(Number(p.price_eur)));
const valueText = (p) => (p.value_eur === null ? "—" : money(p.value_eur, "balance"));
const quantityText = (p, unit = "") => (isMasked("balance") ? MASK : `${QUANTITY.format(Number(p.amount))}${unit}`);
const solAmount = (p) => quantityText(p, " SOL");

function hideButton(p, hidden, ctx) {
  return action(hidden ? "Réafficher" : "Masquer",
    () => ctx.api.put(`/tabs/banque/wallets/hidden-mints/${encodeURIComponent(p.mint)}`, { hidden: !hidden }),
    ctx, { className: "link" });
}

function stakeRow(p, ctx) {
  const name = p.validator_label || short(p.vote_account) || "validateur inconnu";
  const label = el("span", {}, `Staking · ${name}`,
    p.validator_label && p.vote_account ? el("span", { className: "muted", text: ` (${short(p.vote_account)})` }) : null,
    " ", el("span", { className: "muted", text: STAKE_STATES[p.stake_state] || p.stake_state || "" }));
  const rename = p.vote_account
    ? action("Nommer", async () => {
      const value = window.prompt("Nom de ce validateur :", p.validator_label || "");
      if (value === null) return false;
      await ctx.api.put(`/tabs/banque/wallets/validators/${encodeURIComponent(p.vote_account)}`, { label: value });
    }, ctx, { className: "link" })
    : null;
  return row(label, solAmount(p), priceText(p), valueText(p), rename);
}

function tokenRow(p, hidden, ctx) {
  const label = el("span", {}, p.symbol || short(p.mint),
    p.name && p.name !== p.symbol ? el("small", { className: "muted token-name", text: p.name }) : null);
  return row(label, quantityText(p), priceText(p), valueText(p), hideButton(p, hidden, ctx));
}

/** Détail des positions d'un portefeuille (avec « Masquer » et « Nommer ») : affiché dans l'onglet Épargne. */
export function positionsBlock(wallet, ctx) {
  if (!wallet.positions.length) return emptyNote("Aucune position relevée pour l'instant : actualisez.");
  const visible = wallet.positions.filter((p) => !p.hidden);
  const hidden = wallet.positions.filter((p) => p.hidden);
  const tokens = visible.filter((p) => p.kind === "token");
  const priced = tokens.filter((p) => p.value_eur !== null)
    .sort((a, b) => Number(b.value_eur) - Number(a.value_eur));
  const unpriced = tokens.filter((p) => p.value_eur === null);
  const table = (rows) => el("div", { className: "table-wrap scroll-x" },
    el("table", { className: "data-table wallet-table" }, head(["Position", "Quantité", "Prix", "Valeur", ""]),
      el("tbody", {}, rows)));
  return el("div", {},
    table([
      ...visible.filter((p) => p.kind === "sol").map((p) =>
        row("SOL disponible", solAmount(p), priceText(p), valueText(p), null)),
      ...visible.filter((p) => p.kind === "stake").map((p) => stakeRow(p, ctx)),
      ...priced.map((p) => tokenRow(p, false, ctx)),
    ]),
    unpriced.length ? el("details", {}, el("summary", { text: `${unpriced.length} position(s) sans prix` }),
      table(unpriced.map((p) => tokenRow(p, false, ctx)))) : null,
    hidden.length ? el("details", {}, el("summary", { text: `Masqués (${hidden.length})` }),
      table(hidden.map((p) => tokenRow(p, true, ctx)))) : null);
}

/** Gestion seulement : la valeur et les positions se lisent dans l'onglet Épargne. */
function walletsList(wallets, ctx) {
  const head = el("thead", {}, el("tr", {},
    ["Portefeuille", "Adresse", "Dernier relevé", ""].map((h) => el("th", { scope: "col", text: h }))));
  const body = el("tbody", {}, wallets.map((wallet) => el("tr", {},
    el("td", { text: wallet.label }),
    el("td", {}, el("a", { className: "wallet-address", href: SOLSCAN + encodeURIComponent(wallet.address),
      target: "_blank", rel: "noreferrer", text: short(wallet.address), attrs: { title: "Voir sur Solscan" } })),
    el("td", {}, wallet.last_error
      ? el("span", { className: "error", attrs: { role: "alert" }, text: `échec : ${wallet.last_error}` })
      : dateTime(wallet.last_sync_at)),
    el("td", {}, el("div", { className: "filters" },
      action("Renommer", async () => {
        const value = window.prompt("Nouveau nom du portefeuille :", wallet.label);
        if (value === null || !value.trim()) return false;
        await ctx.api.patch(`/tabs/banque/wallets/${wallet.id}`, { label: value.trim() });
      }, ctx, { className: "link" }),
      action("Retirer", () => ctx.api.delete(`/tabs/banque/wallets/${wallet.id}`), ctx, { className: "link danger",
        confirm: `Retirer « ${wallet.label} » ? Son suivi et son historique sont supprimés ; la catégorie `
          + "d'épargne et les virements déjà importés sont conservés." }))))));
  return el("div", { className: "table-wrap scroll-x" }, el("table", { className: "data-table" }, head, body));
}

function addForm(ctx, configured) {
  const label = el("input", { type: "text", placeholder: "Nom (ex. Principal)",
    attrs: { "aria-label": "Nom du portefeuille" } });
  const address = el("input", { type: "text", placeholder: "Adresse Solana (lecture seule)",
    attrs: { "aria-label": "Adresse du portefeuille", autocomplete: "off", spellcheck: "false" } });
  const add = action("Ajouter", async () => {
    if (!label.value.trim() || !address.value.trim()) throw new Error("renseignez un nom et une adresse");
    const r = await ctx.api.post("/tabs/banque/wallets", { label: label.value.trim(), address: address.value.trim() });
    if (r.refresh) return refreshNotice(r.refresh);
    return configured ? "Portefeuille ajouté ; lecture en attente, cliquez sur Actualiser." : "Portefeuille ajouté.";
  }, ctx, { className: "primary" });
  return el("details", { className: "card" }, el("summary", { text: "Ajouter une adresse" }),
    el("p", { className: "muted", text: "Seule l'adresse publique est utilisée : aucune clé privée, aucune écriture "
      + "sur la blockchain." }),
    el("div", { className: "filters" }, label, address), add);
}

/** Champ de saisie de la clé Helius : vérifiée par le serveur auprès de Helius avant d'être enregistrée. */
function keyForm(ctx, label) {
  const input = el("input", { type: "password", placeholder: "Clé API Helius",
    attrs: { "aria-label": "Clé API Helius", autocomplete: "off", spellcheck: "false" } });
  const save = action(label, async () => {
    if (!input.value.trim()) throw new Error("collez votre clé Helius");
    const r = await ctx.api.put("/tabs/banque/wallets/key", { api_key: input.value.trim() });
    input.value = "";
    return r.refresh ? `Clé enregistrée. ${refreshNotice(r.refresh)}` : "Clé enregistrée.";
  }, ctx, { className: "primary" });
  return el("div", { className: "filters" }, input, save);
}

function notConfigured(data, ctx) {
  if (!data.can_set_key) {
    return section("Portefeuilles Solana indisponibles",
      el("p", { text: "L'adresse du service Helius configurée sur le serveur est invalide (EXTRANALYZER_HELIUS_URL)." }));
  }
  return section("Portefeuilles Solana : une étape",
    el("p", { text: "Pour lire vos portefeuilles, l'application a besoin d'une clé Helius, gratuite :" }),
    el("ol", {},
      el("li", {}, "créez un compte sur ",
        el("a", { href: "https://dashboard.helius.dev", text: "dashboard.helius.dev",
          attrs: { target: "_blank", rel: "noreferrer" } }), " (offre gratuite) ;"),
      el("li", { text: "copiez votre clé dans « API Keys » ;" }),
      el("li", { text: "collez-la ici." })),
    keyForm(ctx, "Enregistrer la clé"),
    el("p", { className: "muted", text: "Lecture seule : l'application ne lit que des soldes publics. Elle ne vous "
      + "demandera jamais votre phrase de récupération ni de clé privée. La clé Helius est vérifiée puis gardée sur "
      + "votre serveur ; elle n'est jamais réaffichée." }),
    data.wallets.length ? el("ul", { className: "plain-list" }, data.wallets.map((w) =>
      el("li", { text: `${w.label} — ${short(w.address)} (valeur disponible une fois la clé enregistrée)` }))) : null);
}

function keySettings(data, ctx) {
  if (data.key_source !== "app") return null;
  return el("details", { className: "card" }, el("summary", { text: "Clé Helius" }),
    el("p", { className: "muted", text: "Une clé est enregistrée. Pour la changer, collez la nouvelle :" }),
    keyForm(ctx, "Remplacer la clé"),
    action("Supprimer la clé", () => ctx.api.delete("/tabs/banque/wallets/key"), ctx, { className: "danger",
      confirm: "Supprimer la clé Helius ? Les portefeuilles et leur historique sont conservés, mais ne seront plus "
        + "actualisés." }));
}

function header(data, notice, ctx) {
  return el("section", { className: "card" },
    el("div", { className: "wallets-head" },
      el("div", {},
        el("h2", { text: "Portefeuilles Solana" }),
        el("p", { className: "muted", text: `Dernière actualisation : ${dateTime(data.last_refresh)}` })),
      data.wallets.length
        ? action("Actualiser", async () => refreshNotice(await ctx.api.post("/tabs/banque/wallets/refresh", {})), ctx,
          { className: "primary" })
        : null),
    notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
    data.wallets.length ? walletsList(data.wallets, ctx) : emptyNote("Aucun portefeuille : ajoutez une adresse ci-dessous."),
    data.wallets.length && hasTab(ctx, "epargne")
      ? el("p", {}, el("button", { type: "button", className: "link",
        text: "Valeur, positions et évolution : voir l'onglet Épargne", on: { click: () => ctx.navigate("epargne") } }))
      : null);
}

export async function walletsSection(ctx) {
  const data = await ctx.api.get("/tabs/banque/wallets");
  const notice = lastNotice;
  lastNotice = null;
  if (!data.configured) {
    return el("div", {}, notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
      notConfigured(data, ctx), addForm(ctx, false));
  }
  return el("div", {}, header(data, notice, ctx), addForm(ctx, true), keySettings(data, ctx));
}
