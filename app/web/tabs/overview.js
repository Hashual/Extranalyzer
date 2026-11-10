// Onglet « Vue d'ensemble » : tuiles de totaux, répartition des dépenses, évolution mensuelle,
// plus grosses hausses et dépenses récurrentes.
import { cssVar, mountChart, palette } from "../charts.js";
import { el, emptyNote, formatMonth, money, percent, scopeParams, section } from "../ui.js";

export const id = "overview";
export const title = "Vue d'ensemble";

// sens : +1 si une hausse est une bonne nouvelle, −1 si c'est une mauvaise.
const TILES = [
  { key: "expenses", label: "Dépenses", sense: -1, unclassified: "unclassified_debits" },
  { key: "income", label: "Revenus", sense: 1, unclassified: "unclassified_credits" },
  { key: "savings_net", label: "Épargne", sense: 1 },
  { key: "balance", label: "Solde de la période", sense: 1 },
];

function variation(current, previous, sense) {
  const cur = Number(current);
  const prev = Number(previous);
  const delta = cur - prev;
  const text = prev === 0 ? `${delta > 0 ? "+" : ""}${money(delta)}`
    : `${percent(delta / Math.abs(prev))} (${delta > 0 ? "+" : ""}${money(delta)})`;
  const tone = delta === 0 ? "" : delta * sense > 0 ? "good" : "bad";
  return el("p", { className: `variation ${tone}`.trim(), text: `${text} vs comparaison` });
}

function tiles(data) {
  return el("div", { className: "tiles" }, TILES.map((t) => {
    const tile = el("div", { className: "tile" },
      el("p", { className: "tile-label", text: t.label }),
      el("p", { className: "tile-value", text: money(data.totals[t.key]) }));
    if (data.compare_totals) tile.append(variation(data.totals[t.key], data.compare_totals[t.key], t.sense));
    if (t.unclassified && Number(data.totals[t.unclassified]) > 0) {
      tile.append(el("p", { className: "tile-note", text: `dont ${money(data.totals[t.unclassified])} à classer` }));
    }
    return tile;
  }));
}

function openCategory(ctx, category) {
  if (category.id === null) ctx.navigate("transactions", { category: "none" });
  else ctx.navigate("categories", { id: category.id });
}

/** Couleur stable par nom de catégorie (« À classer » en gris), partagée par les trois graphiques. */
function colorMap(names) {
  const colors = palette(names.length);
  const map = new Map(names.map((name, i) => [name, colors[i]]));
  map.set("À classer", cssVar("--muted"));
  return (name) => map.get(name) || cssVar("--muted");
}

function categoryBars(ctx, categories, colorOf) {
  const max = Math.max(...categories.map((c) => Number(c.amount)), 0);
  return el("ul", { className: "bars" }, categories.map((c) => {
    const fill = el("span", { className: "bar-fill" });
    fill.style.width = `${max > 0 ? (Number(c.amount) / max) * 100 : 0}%`;
    fill.style.background = colorOf(c.name);
    return el("li", {}, el("button", { type: "button", className: "bar-row-button", on: { click: () => openCategory(ctx, c) } },
      el("span", { className: "bar-name", text: c.name }),
      el("span", { className: "bar-track" }, fill),
      el("span", { className: "bar-amount", text: money(c.amount) })));
  }));
}

function pieChart(ctx, host, categories, colorOf) {
  const colors = categories.map((c) => colorOf(c.name));
  mountChart(host, {
    type: "doughnut",
    data: {
      labels: categories.map((c) => c.name),
      datasets: [{ data: categories.map((c) => Number(c.amount)), backgroundColor: colors,
                   borderColor: cssVar("--surface") }],
    },
    options: {
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { label: (item) => `${item.label} : ${money(item.raw)}` } },
      },
      onClick: (_event, elements) => {
        if (elements.length) openCategory(ctx, categories[elements[0].index]);
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
  }));
  datasets.push({
    type: "line",
    label: "Épargne nette",
    data: monthly.map((m) => Number(m.savings_net)),
    borderColor: cssVar("--success"),
    backgroundColor: cssVar("--success"),
    order: 1,
  });
  mountChart(host, {
    data: { labels: monthly.map((m) => formatMonth(m.month)), datasets },
    options: {
      scales: { x: { stacked: true }, y: { stacked: true, ticks: { callback: (v) => money(v) } } },
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { label: (item) => `${item.dataset.label} : ${money(item.raw)}` } },
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
    el("span", { text: row.name }), el("span", { className: "amount neg", text: `+${money(row.increase)}` })),
  "Aucune hausse par rapport à la période " + (ctx.compare ? "de comparaison." : "précédente."));
  const recurring = simpleList(data.recurring, (row) => el("li", {},
    el("span", { className: "label", text: row.label_norm }),
    el("span", { className: "muted", text: `${row.months.length} mois` }),
    el("span", { className: "amount", text: `${money(row.avg_amount)} / mois` })),
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
