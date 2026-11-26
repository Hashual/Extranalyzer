// Onglet « Épargne » : tableau de bord des avoirs (livrets + portefeuilles crypto), répartition, évolution,
// cartes par endroit ; les flux de la période (versements, retraits, net) sont dans une section repliable.
// Les avoirs passent par money(…, "balance") et les flux par money(…, "savings") : tout suit le masquage.
import { cssVar, mountChart, palette } from "../charts.js";
import { amount, confirmDialog, el, emptyNote, formatDate, formatMonth, hasTab, money, promptDialog,
  scopeParams, section, signedMoney } from "../ui.js";
import { isMasked, MASK } from "../privacy.js";
import { positionsBlock } from "./banque-wallets.js";

export const id = "epargne";
export const title = "Épargne";

const PCT = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 1 });

let allocationView = "by_place";        // choix de l'anneau, gardé d'un rafraîchissement à l'autre
let flowsOpen = false;                  // section « Mouvements de la période » dépliée ou non
let openWallet = null;                  // portefeuille dont le détail des positions est déplié (id)

/** Détail des positions du portefeuille déplié (masquer un jeton, nommer un validateur). */
async function walletDetail(ctx, places) {
  const place = places.find((p) => p.kind === "wallet" && p.id === openWallet);
  if (!place) {
    openWallet = null;
    return null;
  }
  let data;
  try {
    data = await ctx.api.get("/tabs/banque/wallets");
  } catch (e) {               // le détail ne doit jamais empêcher l'affichage du tableau de bord
    openWallet = null;
    return el("p", { className: "error", attrs: { role: "alert" }, text: `Détail indisponible : ${e.message}` });
  }
  const wallet = data.wallets.find((w) => w.id === openWallet);
  if (!wallet) {
    openWallet = null;
    return null;
  }
  return section(`Positions · ${wallet.label}`,
    wallet.last_error ? el("p", { className: "error", attrs: { role: "alert" },
      text: `Dernière actualisation en échec : ${wallet.last_error}` }) : null,
    positionsBlock(wallet, ctx));
}

/** "2025-01-31" → "31/01" (ou "31/01/25" quand la courbe couvre plusieurs années). */
const dayMonth = (iso, withYear = false) =>
  `${iso.slice(8, 10)}/${iso.slice(5, 7)}${withYear ? `/${iso.slice(2, 4)}` : ""}`;

function tile(label, value, ...notes) {
  return el("div", { className: "tile" },
    el("p", { className: "tile-label", text: label }),
    el("p", { className: "tile-value", text: value }),
    ...notes);
}

function note(text, className = "variation", tag = "p") {
  return el(tag, { className, text });
}

/** Variation signée, colorée sauf en mode discret (la couleur révélerait le signe). */
function variationNote(change, suffix = "", tag = "p") {
  if (change === null) return null;
  const sign = Number(change) < 0 ? "bad" : Number(change) > 0 ? "good" : "";
  return note(`${signedMoney(change, "balance")}${suffix}`, `variation ${isMasked("balance") ? "" : sign}`.trim(), tag);
}

/** Écart signé (+/−), coloré sauf en mode discret. */
function signedGap(value) {
  const masked = isMasked("balance");
  const cls = masked ? "" : Number(value) < 0 ? " neg" : Number(value) > 0 ? " pos" : "";
  return el("span", { className: `amount${cls}`, text: signedMoney(value, "balance") });
}

function dashboardTiles(d) {
  const { total, savings, crypto } = d;
  const partial = total.change_scope === "partial";
  const known = d.places.filter((p) => p.value !== null);
  const excludedPlaces = known.filter((p) => p.change === null);
  const cryptoOnlyExcluded = excludedPlaces.length > 0 && excludedPlaces.every((p) => p.kind === "wallet")
    && !known.some((p) => p.kind === "wallet" && p.change !== null);
  const since = crypto.tracked_since ? ` depuis le ${formatDate(crypto.tracked_since)}` : "";
  const scope = crypto.tracked_since && cryptoOnlyExcluded ? ` (livrets seuls, crypto suivie${since})`
    : " (sur une partie des comptes)";
  const unknownPlaces = savings.unknown_count + crypto.unread_count;
  const totalHint = unknownPlaces ? note(`hors ${unknownPlaces} endroit(s) sans valeur`, "tile-note") : null;
  const totalChange = variationNote(total.change, partial ? scope : " sur la période");

  const savingsNotes = [note(`${savings.count} compte(s)`),
    savings.unknown_count ? note(`hors ${savings.unknown_count} sans solde connu`, "tile-note") : null];
  const cryptoNotes = [note(`${crypto.count} portefeuille(s)`),
    crypto.unread_count ? note(`hors ${crypto.unread_count} sans relevé`, "tile-note") : null,
    crypto.unpriced_count ? note(`hors ${crypto.unpriced_count} position(s) sans prix`, "tile-note") : null];

  // Portefeuilles à valeur connue mais sans versé comparable (pas de catégorie d'épargne) : hors comparaison.
  const excluded = d.places.filter((p) => p.kind === "wallet" && p.value !== null && p.deposited === null).length;
  const gapNote = crypto.gap === null ? null
    : el("p", { className: "variation" },
      el("span", { text: "Écart " }), signedGap(crypto.gap),
      crypto.compared_partial
        ? el("span", { className: "muted", text: excluded ? ` (hors ${excluded} portefeuille(s))` : " (partiel)" })
        : null);
  const comparison = crypto.deposited === null || crypto.compared_value === null
    ? "—" : `${money(crypto.deposited, "balance")} → ${money(crypto.compared_value, "balance")}`;

  return el("div", { className: "tiles" },
    tile("Épargne totale", total.value === null ? "—" : money(total.value, "balance"), totalChange, totalHint),
    tile("Livrets", savings.value === null ? "—" : money(savings.value, "balance"), ...savingsNotes),
    tile("Crypto", crypto.value === null ? "—" : money(crypto.value, "balance"), ...cryptoNotes),
    tile("Versé → valeur", comparison, gapNote));
}

/**
 * Pastille de couleur : la couleur passe au CSS par la propriété --swatch, réglée par le CSSOM
 * (un attribut style en ligne serait bloqué par la CSP).
 */
function swatch(color) {
  const node = el("span", { className: "swatch" });
  node.style.setProperty("--swatch", color);
  return node;
}

function allocationCard(d) {
  const host = el("div", { className: "chart chart-donut" });
  const legend = el("ul", { className: "plain-list donut-legend" });
  const buttons = el("div", { className: "toggle", attrs: { role: "group", "aria-label": "Répartition par" } });
  const views = [["by_place", "Par endroit"], ["by_asset", "Par actif"]];

  function draw() {
    const slices = d.allocation[allocationView].filter((s) => Number(s.value) > 0);
    for (const b of buttons.children) b.setAttribute("aria-pressed", String(b.dataset.view === allocationView));
    if (!slices.length) {
      host.replaceChildren(emptyNote("Rien à répartir."));
      legend.replaceChildren();
      return;
    }
    const colors = palette(slices.length);
    const total = slices.reduce((sum, s) => sum + Number(s.value), 0);
    const masked = isMasked("balance");
    mountChart(host, {
      type: "doughnut",
      data: { labels: slices.map((s) => s.name),
        datasets: [{ data: slices.map((s) => Number(s.value)), backgroundColor: colors,
          borderColor: cssVar("--surface"), borderWidth: 2 }] },
      options: { plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (item) => `${item.label} : ${money(item.raw, "balance")}` } } } },
    });
    legend.replaceChildren(...slices.map((s, i) => el("li", {},
      swatch(colors[i]),
      el("span", { className: "legend-name", text: s.name }),
      el("span", { className: "num muted", text: masked ? MASK : PCT.format(Number(s.value) / total) }))));
  }

  for (const [view, label] of views) {
    buttons.append(el("button", { type: "button", text: label, dataset: { view },
      on: { click: () => { allocationView = view; draw(); } } }));
  }
  const card = section("Répartition", buttons, host, legend);
  requestAnimationFrame(() => { if (host.isConnected) draw(); });   // le canevas doit être dans la page pour se dimensionner
  return card;
}

function evolutionCard(d) {
  const host = el("div", { className: "chart" });
  const card = section("Évolution", host);
  const hasData = d.series.some((p) => p.savings !== null || p.crypto !== null);
  if (d.series.length < 2 || !hasData) {
    host.replaceChildren(emptyNote("Pas encore assez de relevés pour tracer une courbe."));
  } else {
    const multiYear = d.series[0].date.slice(0, 4) !== d.series[d.series.length - 1].date.slice(0, 4);
    const num = (v) => (v === null ? null : Number(v));
    const layer = (label, key, color, fill) => ({ label, data: d.series.map((p) => num(p[key])), fill,
      borderColor: color, backgroundColor: color + "55", pointRadius: 0, tension: 0.2, spanGaps: false });
    requestAnimationFrame(() => {
      if (!host.isConnected) return;
      mountChart(host, {
        type: "line",
        data: { labels: d.series.map((p) => dayMonth(p.date, multiYear)),
          datasets: [layer("Livrets", "savings", cssVar("--chart-1"), "origin"),
            layer("Crypto", "crypto", cssVar("--chart-2"), "-1")] },
        options: {
          interaction: { mode: "index", intersect: false },
          scales: { y: { stacked: true, beginAtZero: true, ticks: { callback: (v) => money(v, "balance") } } },
          plugins: { legend: { position: "bottom" },
            tooltip: { callbacks: { title: (items) => formatDate(d.series[items[0].dataIndex].date),
              label: (item) => `${item.dataset.label} : ${item.raw === null ? "—" : money(item.raw, "balance")}` } } },
        },
      });
    });
  }
  if (d.crypto.tracked_since) {
    card.append(el("p", { className: "muted", text: `Crypto suivie depuis le ${formatDate(d.crypto.tracked_since)}.` }));
  }
  return card;
}

/** Carte d'un livret ou d'un portefeuille : même structure (nom, valeur, variation, ligne d'info, actions). */
function placeCard(p, ctx) {
  const wallet = p.kind === "wallet";
  const canDetail = wallet && hasTab(ctx, "banque");
  const open = wallet && openWallet === p.id;
  const toggleDetail = () => { openWallet = open ? null : p.id; ctx.refresh(); };
  const target = wallet ? (canDetail ? toggleDetail : null)
    : p.category_id !== null && hasTab(ctx, "categories") ? () => ctx.navigate("categories", { id: p.category_id })
      : null;
  const lines = [
    el("span", { className: "place-name", text: p.name }),
    p.value === null ? el("span", { className: "place-value muted", text: "valeur inconnue" })
      : el("span", { className: "place-value", text: money(p.value, "balance") }),
    variationNote(p.change, " sur la période", "span") || el("span", { className: "variation muted", text: "—" }),
    wallet ? walletInfo(p) : el("span", { className: "muted place-info", text: p.balance_source ? balanceOrigin(p) : "" }),
  ];
  const card = target
    ? el("button", { type: "button", className: `place-card clickable${open ? " open" : ""}`, on: { click: target },
      attrs: wallet ? { "aria-expanded": String(open), title: "Voir le détail des positions" }
        : { title: "Voir la catégorie" } },
    ...lines)
    : el("div", { className: "place-card" }, ...lines);
  const actions = wallet
    ? el("div", { className: "place-actions" }, canDetail
      ? el("button", { type: "button", className: "link", text: open ? "Masquer les positions" : "Voir les positions",
        on: { click: toggleDetail } })
      : null)
    : balanceActions(p, ctx);
  return el("div", { className: "place-wrap" }, card, actions);
}

function walletInfo(p) {
  if (p.deposited === null) return el("span", { className: "muted place-info", text: "versé inconnu" });
  return el("span", { className: "place-info" }, `Versé ${money(p.deposited, "balance")}`,
    p.gap !== null ? el("span", {}, " · écart ", signedGap(p.gap)) : null);
}

/** Livrets et portefeuilles crypto côte à côte, chacun dans son bloc. */
function placesBlocks(d, ctx) {
  const livrets = d.places.filter((p) => p.kind === "savings");
  const wallets = d.places.filter((p) => p.kind === "wallet");
  const cards = (list) => el("div", { className: "place-cards" }, list.map((p) => placeCard(p, ctx)));
  return el("div", { className: "grid-2 places-cols" },
    section("Livrets", livrets.length ? cards(livrets)
      : emptyNote("Aucun livret : déposez ses relevés PDF avec « Déposer des relevés ».")),
    section("Crypto", wallets.length ? cards(wallets)
      : el("p", { className: "muted" }, "Aucun portefeuille suivi. ", hasTab(ctx, "banque")
        ? el("button", { type: "button", className: "link", text: "Ajouter un portefeuille",
          on: { click: () => ctx.navigate("banque") } }) : null)));
}

const BALANCE_ORIGINS = {
  statement: (d) => `relevé du ${d}`,
  estimated: (d) => `relevé du ${d} + virements depuis`,
  bank: (d) => `solde de la banque au ${d}`,
  manual: (d) => `solde saisi pour le ${d}`,
};

function balanceOrigin(p) {
  const when = p.balance_as_of ? formatDate(p.balance_as_of) : "?";
  return (BALANCE_ORIGINS[p.balance_source] || (() => ""))(when);
}

/** Corriger le solde réel d'un livret (il fait foi), ou revenir au calcul automatique. */
function balanceActions(p, ctx) {
  const error = el("span", { className: "error", attrs: { role: "alert" } });
  const run = async (fn) => {
    error.textContent = "";
    try {
      if ((await fn()) !== false) await ctx.refresh();
    } catch (e) {
      error.textContent = e.message;
    }
  };
  const fix = el("button", { type: "button", className: "link", text: "Corriger le solde", on: { click: () => run(async () => {
    const value = await promptDialog(`Solde réel de « ${p.name} » aujourd'hui (en euros) :`, { inputMode: "decimal" });
    if (value === null || !value.trim()) return false;
    await ctx.api.put(`/tabs/epargne/balance/${p.id}`, { amount: value.trim() });
  }) } });
  const reset = p.balance_source === "manual"
    ? el("button", { type: "button", className: "link", text: "Revenir au calcul automatique", on: { click: () => run(async () => {
      if (!(await confirmDialog("Oublier le solde saisi ? Le montant sera de nouveau calculé."))) return false;
      await ctx.api.delete(`/tabs/epargne/balance/${p.id}`);
    }) } })
    : null;
  return el("div", { className: "place-actions" }, fix, reset, error);
}

// ---- Cryptos : répartition par catégorie (actif seul, ou groupe : « Altcoins », fusion WBTC + BTC…) ----

const ALTCOINS = "Altcoins";
const ALTCOINS_KEY = `group:${ALTCOINS}`;

/** Lance une modification puis rafraîchit l'onglet ; l'erreur s'affiche dans `error`. */
async function mutate(ctx, error, fn) {
  error.textContent = "";
  try {
    if ((await fn()) !== false) await ctx.refresh();
  } catch (e) {
    error.textContent = e.message;
  }
}

const setGroup = (ctx, key, group) =>
  ctx.api.put(`/tabs/epargne/crypto/group/${encodeURIComponent(key)}`, { group });

/** Menu « Fusionner avec… » : la ligne rejoint la cible choisie (actif, groupe, ou « Altcoins »). */
function mergePicker(ctx, error, cat, cats) {
  const targets = cats.filter((c) => c.key !== cat.key);
  if (!cats.some((c) => c.key === ALTCOINS_KEY) && cat.key !== ALTCOINS_KEY) {
    targets.push({ key: ALTCOINS_KEY, name: ALTCOINS, group: true, members: [] });
  }
  if (!targets.length) return null;
  const select = el("select", { className: "merge-picker", attrs: { "aria-label": `Fusionner ${cat.name} avec` } },
    el("option", { value: "", text: "Fusionner avec…" }),
    targets.map((t) => el("option", { value: t.key, text: t.group ? `${t.name} (groupe)` : t.name })));
  select.addEventListener("change", () => {
    const target = targets.find((t) => t.key === select.value);
    if (!target) return;
    select.disabled = true;
    mutate(ctx, error, () => ctx.api.post("/tabs/epargne/crypto/merge",
      { source: cat.key, target: target.key, name: target.name }))
      .finally(() => { select.disabled = false; select.value = ""; });
  });
  return select;
}

function cryptoAllocationCard(d, ctx) {
  const cats = d.crypto_categories.filter((c) => Number(c.value) > 0);
  const host = el("div", { className: "chart chart-donut" });
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const colors = palette(cats.length);
  const total = cats.reduce((sum, c) => sum + Number(c.value), 0);
  const masked = isMasked("balance");
  const share = (value) => (masked ? MASK : PCT.format(Number(value) / total));
  const link = (text, title, fn) => el("button", { type: "button", className: "link", text, attrs: { title },
    on: { click: () => mutate(ctx, error, fn) } });

  const legend = el("ul", { className: "plain-list donut-legend crypto-legend" }, cats.map((c, i) => el("li", {},
    el("div", { className: "legend-row" },
      swatch(colors[i]),
      el("span", { className: "legend-name", text: c.group ? `${c.name} (${c.members.length})` : c.name }),
      el("span", { className: "num muted", text: share(c.value) }),
      mergePicker(ctx, error, c, cats)),
    c.group ? el("ul", { className: "plain-list group-members" }, c.members.map((m) => el("li", {},
      el("span", { className: "legend-name", text: m.name }),
      el("span", { className: "num muted", text: share(m.value) }),
      link("Retirer", `Sortir ${m.name} de ${c.name}`, () => setGroup(ctx, m.key, null))))) : null)));

  const card = section("Répartition des cryptos", host, legend, error,
    el("p", { className: "muted small", text: "« Fusionner avec… » réunit deux cryptos en une seule part (ex. WBTC "
      + `avec BTC) ; rangez les petites positions dans « ${ALTCOINS} ». « Retirer » défait la fusion.` }));
  if (!cats.length) {
    host.replaceChildren(emptyNote("Aucune crypto valorisée."));
    return card;
  }
  requestAnimationFrame(() => {
    if (!host.isConnected) return;
    mountChart(host, {
      type: "doughnut",
      data: { labels: cats.map((c) => c.name),
        datasets: [{ data: cats.map((c) => Number(c.value)), backgroundColor: colors,
          borderColor: cssVar("--surface"), borderWidth: 2 }] },
      options: { plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (item) => `${item.label} : ${money(item.raw, "balance")}` } } } },
    });
  });
  return card;
}

function cryptoSection(d, ctx) {
  if (!d.crypto_categories.length) return null;
  return el("div", { className: "grid-2 dash-cols" }, cryptoAllocationCard(d, ctx));
}

function flowsTiles(total) {
  return el("div", { className: "tiles" },
    [["Versements", total.deposits], ["Retraits", total.withdrawals], ["Épargne nette", total.net]].map(
      ([label, value]) => el("div", { className: "tile" },
        el("p", { className: "tile-label", text: label }),
        el("p", { className: "tile-value", text: money(value, "savings") }))));
}

function destinationsTable(ctx, destinations) {
  const head = el("thead", {}, el("tr", {},
    ["Destination", "Versements", "Retraits", "Net"].map((h, i) =>
      el("th", { scope: "col", className: i ? "num" : "", text: h }))));
  const body = el("tbody", {}, destinations.map((d) => el("tr", {},
    el("td", {}, hasTab(ctx, "categories") && d.id !== null
      ? el("button", { type: "button", className: "link", text: d.name,
                       on: { click: () => ctx.navigate("categories", { id: d.id }) } })
      : d.name),
    el("td", { className: "num", text: money(d.deposits, "savings") }),
    el("td", { className: "num", text: money(d.withdrawals, "savings") }),
    el("td", { className: "num" }, amount(d.net, "savings")))));
  return el("div", { className: "table-wrap" }, el("table", { className: "data-table" }, head, body));
}

async function fillFlows(body, ctx) {
  const data = await ctx.api.get("/tabs/epargne/summary", scopeParams(ctx));
  const chartHost = el("div", { className: "chart chart-wide" });
  const active = data.destinations.filter((d) => Number(d.deposits) || Number(d.withdrawals));
  body.replaceChildren(
    flowsTiles(data.total),
    section("Épargne nette cumulée", chartHost),
    section("Par destination", data.destinations.length ? destinationsTable(ctx, data.destinations)
      : emptyNote("Aucune catégorie d'épargne définie.")),
    ...(active.length ? [] : [emptyNote("Aucun mouvement d'épargne sur la période (comptes sélectionnés).")]));

  mountChart(chartHost, {
    data: {
      labels: data.monthly.map((m) => formatMonth(m.month)),
      datasets: [
        { type: "bar", label: "Net du mois", data: data.monthly.map((m) => Number(m.net)),
          backgroundColor: cssVar("--chart-2"), order: 2 },
        { type: "line", label: "Cumul", data: data.monthly.map((m) => Number(m.net_cumulative)),
          borderColor: cssVar("--success"), backgroundColor: cssVar("--success"), order: 1 },
      ],
    },
    options: {
      scales: { y: { ticks: { callback: (v) => money(v, "savings") } } },
      plugins: { legend: { position: "bottom" },
                 tooltip: { callbacks: { label: (item) => `${item.dataset.label} : ${money(item.raw, "savings")}` } } },
    },
  });
}

/** Section repliable : les flux ne sont chargés (et les graphiques dimensionnés) qu'à l'ouverture. */
function flowsSection(ctx) {
  const body = el("div", { className: "flows-body" });
  const details = el("details", { className: "card flows", open: flowsOpen },
    el("summary", { text: "Mouvements de la période" }), body);
  let loaded = false;
  const load = async () => {
    if (loaded || !details.open) return;
    loaded = true;
    try {
      await fillFlows(body, ctx);
    } catch (e) {
      loaded = false;
      body.replaceChildren(el("p", { className: "error", attrs: { role: "alert" }, text: e.message }));
    }
  };
  details.addEventListener("toggle", () => { flowsOpen = details.open; load(); });
  if (details.open) requestAnimationFrame(load);
  return details;
}

function welcome(ctx) {
  return section("Bienvenue dans Épargne",
    el("p", { text: "Rien à afficher pour l'instant : aucun livret ni portefeuille crypto n'est suivi." }),
    el("ul", {},
      el("li", {}, "Suivez un portefeuille Solana : ",
        hasTab(ctx, "banque")
          ? el("button", { type: "button", className: "link", text: "ajouter un portefeuille",
            on: { click: () => ctx.navigate("banque") } })
          : "onglet Connexions"),
      el("li", { text: "Ou déposez les relevés PDF de vos livrets avec le bouton « Déposer des relevés » en haut de la page." })));
}

export async function render(container, ctx) {
  const d = await ctx.api.get("/tabs/epargne/dashboard", { start: ctx.period.from, end: ctx.period.to });
  if (!d.places.length) {
    container.replaceChildren(welcome(ctx), flowsSection(ctx));
    return;
  }
  const detail = await walletDetail(ctx, d.places);
  container.replaceChildren(...[
    dashboardTiles(d),
    el("div", { className: "grid-2 dash-cols" }, allocationCard(d), evolutionCard(d)),
    placesBlocks(d, ctx),
    detail,
    cryptoSection(d, ctx),
    flowsSection(ctx),
  ].filter(Boolean));
}
