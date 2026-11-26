// Onglet « Vue d'ensemble » : tuiles de totaux, répartition des dépenses, évolution mensuelle,
// plus grosses hausses et dépenses récurrentes.
import { cssVar, mountChart, palette } from "../charts.js";
import { isMasked } from "../privacy.js";
import { el, emptyNote, formatMonth, hasTab, money, percent, scopeParams, section, setFill, signedMoney } from "../ui.js";

export const id = "overview";
export const title = "Vue d'ensemble";

// sens : +1 si une hausse est une bonne nouvelle, −1 si c'est une mauvaise.
const TILES = [
  // Les débits à classer comptent dans les Dépenses ; les crédits à classer restent hors des Revenus.
  { key: "expenses", label: "Dépenses", kind: "expense", sense: -1, unclassified: "unclassified_debits",
    note: (x) => `dont ${money(x, "expense")} à classer` },
  { key: "income", label: "Revenus", kind: "income", sense: 1, unclassified: "unclassified_credits",
    note: (x) => `+ ${money(x, "income")} de crédits à classer` },
  { key: "savings_net", label: "Épargne", kind: "savings", sense: 1 },
  { key: "balance", label: "Solde de la période", kind: "balance", sense: 1 },
];

function variation(current, previous, sense, kind) {
  const cur = Number(current);
  const prev = Number(previous);
  const delta = cur - prev;
  const text = prev === 0 ? signedMoney(delta, kind)
    : `${percent(delta / Math.abs(prev))} (${signedMoney(delta, kind)})`;
  const tone = delta === 0 ? "" : delta * sense > 0 ? "good" : "bad";
  return el("p", { className: `variation ${tone}`.trim(), text: `${text} vs comparaison` });
}

function tiles(data) {
  return el("div", { className: "tiles" }, TILES.map((t) => {
    const tile = el("div", { className: "tile" },
      el("p", { className: "tile-label", text: t.label }),
      el("p", { className: "tile-value", text: money(data.totals[t.key], t.kind) }));
    if (data.compare_totals) tile.append(variation(data.totals[t.key], data.compare_totals[t.key], t.sense, t.kind));
    if (t.unclassified && Number(data.totals[t.unclassified]) > 0) {
      tile.append(el("p", { className: "tile-note", text: t.note(data.totals[t.unclassified]) }));
    }
    return tile;
  }));
}

/** Lien d'une catégorie (Catégories, ou Transactions « à classer ») ; null si l'onglet est désactivé. */
function categoryLink(ctx, category) {
  if (category.id === null) {
    return hasTab(ctx, "transactions") ? () => ctx.navigate("transactions", { category: "none" }) : null;
  }
  return hasTab(ctx, "categories") ? () => ctx.navigate("categories", { id: category.id }) : null;
}

/** Montant affiché dans un graphique : jamais négatif (remboursements supérieurs aux dépenses). */
const plotted = (value) => Math.max(Number(value), 0);

/** Couleur stable par nom de catégorie (« À classer » en gris), partagée par les trois graphiques. */
function colorMap(names) {
  const colors = palette(names.length);
  const map = new Map(names.map((name, i) => [name, colors[i]]));
  map.set("À classer", cssVar("--muted"));
  return (name) => map.get(name) || cssVar("--muted");
}

function categoryBars(ctx, categories, colorOf) {
  const max = Math.max(...categories.map((c) => plotted(c.amount)), 0);
  return el("ul", { className: "bars" }, categories.map((c) => {
    const fill = setFill(el("span", { className: "bar-fill" }), max > 0 ? plotted(c.amount) / max : 0, colorOf(c.name));
    const open = categoryLink(ctx, c);
    return el("li", {}, el("button", { type: "button", className: "bar-row-button", disabled: !open,
                                       on: { click: () => open && open() } },
      el("span", { className: "bar-name", text: c.name }),
      el("span", { className: "bar-track" }, fill),
      el("span", { className: "bar-amount", text: money(c.amount, "expense") })));
  }));
}

function pieChart(ctx, host, categories, colorOf) {
  const colors = categories.map((c) => colorOf(c.name));
  mountChart(host, {
    type: "doughnut",
    data: {
      labels: categories.map((c) => c.name),
      datasets: [{ data: categories.map((c) => plotted(c.amount)), backgroundColor: colors,
                   borderColor: cssVar("--surface") }],
    },
    options: {
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { label: (item) => `${item.label} : ${money(item.raw, "expense")}` } },
      },
      onClick: (_event, elements) => {
        const open = elements.length ? categoryLink(ctx, categories[elements[0].index]) : null;
        if (open) open();
      },
    },
  });
}

function monthlyChart(host, monthly, colorOf) {
  const names = [...new Set(monthly.flatMap((m) => Object.keys(m.by_category)))];
  const datasets = names.map((name) => ({
    type: "bar",
    label: name,
    data: monthly.map((m) => Number(m.by_category[name] || 0)),
    backgroundColor: colorOf(name),
    stack: "depenses",
    order: 2,
    kind: "expense",
  }));
  datasets.push({
    type: "line",
    label: "Épargne nette",
    data: monthly.map((m) => Number(m.savings_net)),
    borderColor: cssVar("--success"),
    backgroundColor: cssVar("--success"),
    order: 1,
    kind: "savings",
  });
  mountChart(host, {
    data: { labels: monthly.map((m) => formatMonth(m.month)), datasets },
    options: {
      // L'axe porte dépenses et épargne : il est masqué dès que l'un des deux l'est.
      scales: { x: { stacked: true }, y: { stacked: true, ticks: {
        callback: (v) => money(v, isMasked("savings") ? "savings" : "expense") } } },
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { label: (item) => `${item.dataset.label} : ${money(item.raw, item.dataset.kind)}` } },
      },
    },
  });
}

function simpleList(items, render, emptyText) {
  if (!items.length) return emptyNote(emptyText);
  return el("ul", { className: "plain-list" }, items.map(render));
}

export async function render(container, ctx) {
  const data = await ctx.api.get("/tabs/overview/summary", scopeParams(ctx));

  const pieHost = el("div", { className: "chart" });
  const monthlyHost = el("div", { className: "chart chart-wide" });
  const hasExpenses = data.by_category.length > 0;
  const names = [...new Set([...data.by_category.map((c) => c.name),
                             ...data.monthly.flatMap((m) => Object.keys(m.by_category))])];
  const colorOf = colorMap(names);

  const increases = simpleList(data.top_increases, (row) => el("li", {},
    el("span", { text: row.name }), el("span", { className: "amount neg", text: signedMoney(row.increase, "expense") })),
  "Aucune hausse par rapport à la période " + (ctx.compare ? "de comparaison." : "précédente."));
  const recurring = simpleList(data.recurring, (row) => el("li", {},
    el("span", { className: "label", text: row.label_norm }),
    el("span", { className: "muted", text: `${row.months.length} mois` }),
    el("span", { className: "amount", text: `${money(row.avg_amount, "expense")} / mois` })),
  "Aucune dépense récurrente détectée sur la période.");

  container.replaceChildren(
    tiles(data),
    el("div", { className: "grid-2" },
      section("Répartition des dépenses", hasExpenses ? pieHost : emptyNote("Aucune dépense sur la période.")),
      section("Dépenses par catégorie", hasExpenses ? categoryBars(ctx, data.by_category, colorOf)
        : emptyNote("Aucune dépense sur la période."))),
    section("Évolution mois par mois", monthlyHost),
    el("div", { className: "grid-2" },
      section(ctx.compare ? "Plus grosses hausses (vs comparaison)" : "Plus grosses hausses (vs période précédente)",
        increases),
      section("Dépenses récurrentes", recurring)));

  if (hasExpenses) pieChart(ctx, pieHost, data.by_category, colorOf);
  monthlyChart(monthlyHost, data.monthly, colorOf);
}
