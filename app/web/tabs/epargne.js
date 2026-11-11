// Onglet « Épargne » : versements, retraits et net par destination, et cumul mensuel de l'épargne nette.
import { cssVar, mountChart } from "../charts.js";
import { amount, el, emptyNote, formatMonth, hasTab, money, scopeParams, section } from "../ui.js";

export const id = "epargne";
export const title = "Épargne";

function tiles(total) {
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

export async function render(container, ctx) {
  const data = await ctx.api.get("/tabs/epargne/summary", scopeParams(ctx));
  const chartHost = el("div", { className: "chart chart-wide" });
  const active = data.destinations.filter((d) => Number(d.deposits) || Number(d.withdrawals));

  container.replaceChildren(
    tiles(data.total),
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
