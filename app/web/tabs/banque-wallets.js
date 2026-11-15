// Section « Portefeuilles Solana » de l'onglet Banque : suivi en lecture seule (SOL, jetons, staking) valorisé en euros.
// Tous les montants en euros passent par money(…, "balance") : ils suivent le masquage des soldes.
import { cssVar, mountChart } from "../charts.js";
import { el, emptyNote, money, section } from "../ui.js";
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

function unpricedText(n) {
  return `hors ${n} jeton(s) sans prix`;
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

function positionsBlock(wallet, ctx) {
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
    unpriced.length ? el("details", {}, el("summary", { text: `${unpriced.length} jeton(s) sans prix` }),
      table(unpriced.map((p) => tokenRow(p, false, ctx)))) : null,
    hidden.length ? el("details", {}, el("summary", { text: `Masqués (${hidden.length})` }),
      table(hidden.map((p) => tokenRow(p, true, ctx)))) : null);
}

function historyChart(wallet) {
  if (wallet.history.length < 2) return null;
  const host = el("div", { className: "chart" });
  const masked = isMasked("balance");
  requestAnimationFrame(() => {       // le canevas a besoin d'être dans la page pour se dimensionner
    if (!host.isConnected) return;
    mountChart(host, {
      type: "line",
      data: {
        labels: wallet.history.map((h) => new Date(h.taken_at).toLocaleDateString("fr-FR",
          { day: "2-digit", month: "2-digit" })),
        datasets: [{ label: wallet.label, data: wallet.history.map((h) => Number(h.total_eur)),
          borderColor: cssVar("--accent"), backgroundColor: cssVar("--accent"), pointRadius: 2, tension: 0.2 }],
      },
      options: {
        scales: { y: { ticks: { callback: (v) => (masked ? MASK : money(v, "balance")) } } },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (item) => money(item.raw, "balance") } } },
      },
    });
  });
  return host;
}

function walletCard(wallet, ctx) {
  const title = el("h2", { className: "wallet-title" }, wallet.label, " ",
    el("a", { className: "wallet-address", href: SOLSCAN + encodeURIComponent(wallet.address), target: "_blank",
      rel: "noreferrer", text: short(wallet.address), attrs: { title: "Voir sur Solscan" } }));
  return el("section", { className: "card" }, title,
    el("p", { className: "wallet-total" }, el("strong", { text: money(wallet.total_eur, "balance") }),
      wallet.unpriced_count ? el("span", { className: "muted", text: ` (${unpricedText(wallet.unpriced_count)})` }) : null),
    wallet.category_name
      ? el("p", { className: "muted", text: `Net versé depuis la banque : ${money(wallet.net_deposited, "balance")} `
        + `(Épargne / ${wallet.category_name})` })
      : null,
    wallet.last_error ? el("p", { className: "error", attrs: { role: "alert" },
      text: `Dernière actualisation en échec : ${wallet.last_error}` }) : null,
    el("p", { className: "muted", text: `Dernier relevé : ${dateTime(wallet.last_sync_at)}` }),
    positionsBlock(wallet, ctx),
    historyChart(wallet),
    el("div", { className: "filters" },
      action("Renommer", async () => {
        const value = window.prompt("Nouveau nom du portefeuille :", wallet.label);
        if (value === null || !value.trim()) return false;
        await ctx.api.patch(`/tabs/banque/wallets/${wallet.id}`, { label: value.trim() });
      }, ctx),
      action("Retirer", () => ctx.api.delete(`/tabs/banque/wallets/${wallet.id}`), ctx, { className: "danger",
        confirm: `Retirer « ${wallet.label} » ? Son suivi et son historique sont supprimés ; la catégorie `
          + "d'épargne et les virements déjà importés sont conservés." })));
}

function addForm(ctx) {
  const label = el("input", { type: "text", placeholder: "Nom (ex. Principal)",
    attrs: { "aria-label": "Nom du portefeuille" } });
  const address = el("input", { type: "text", placeholder: "Adresse Solana (lecture seule)",
    attrs: { "aria-label": "Adresse du portefeuille", autocomplete: "off", spellcheck: "false" } });
  const add = action("Ajouter", async () => {
    if (!label.value.trim() || !address.value.trim()) throw new Error("renseignez un nom et une adresse");
    const r = await ctx.api.post("/tabs/banque/wallets", { label: label.value.trim(), address: address.value.trim() });
    return r.refresh ? refreshNotice(r.refresh) : "Portefeuille ajouté.";
  }, ctx, { className: "primary" });
  return el("details", { className: "card" }, el("summary", { text: "Ajouter une adresse" }),
    el("p", { className: "muted", text: "Seule l'adresse publique est utilisée : aucune clé privée, aucune écriture "
      + "sur la blockchain." }),
    el("div", { className: "filters" }, label, address), add);
}

function notConfigured(data) {
  return section("Portefeuilles Solana non configurés",
    el("p", { text: "Pour suivre vos portefeuilles Solana, fournissez au conteneur la clé Helius en secret Docker "
      + "(helius_key, ou le fichier indiqué par EXTRANALYZER_HELIUS_KEY_FILE)." }),
    el("p", { className: "muted", text: "Marche à suivre détaillée : README, section « Portefeuilles Solana »." }),
    data.wallets.length ? el("ul", { className: "plain-list" }, data.wallets.map((w) =>
      el("li", { text: `${w.label} — ${short(w.address)} (valeur disponible une fois la clé configurée)` }))) : null);
}

function header(data, notice, ctx) {
  return el("section", { className: "card" },
    el("div", { className: "wallets-head" },
      el("div", {},
        el("h2", { text: "Portefeuilles Solana" }),
        el("p", {}, "Total : ", el("strong", { text: money(data.total_eur, "balance") }),
          data.unpriced_count ? el("span", { className: "muted", text: ` (${unpricedText(data.unpriced_count)})` }) : null),
        el("p", { className: "muted", text: `Dernière actualisation : ${dateTime(data.last_refresh)}` })),
      data.wallets.length
        ? action("Actualiser", async () => refreshNotice(await ctx.api.post("/tabs/banque/wallets/refresh", {})), ctx,
          { className: "primary" })
        : null),
    notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
    data.wallets.length ? null : emptyNote("Aucun portefeuille : ajoutez une adresse ci-dessous."));
}

export async function walletsSection(ctx) {
  const data = await ctx.api.get("/tabs/banque/wallets");
  const notice = lastNotice;
  lastNotice = null;
  if (!data.configured) {
    return el("div", {}, notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null,
      notConfigured(data), addForm(ctx));
  }
  return el("div", {}, header(data, notice, ctx), ...data.wallets.map((w) => walletCard(w, ctx)), addForm(ctx));
}
