// Onglet « Budget » : suivi des objectifs mensuels par catégorie (dépensé, attendu à ce jour, projection),
// modification des objectifs (datés : « à partir de tel mois », les mois passés gardent les leurs), assistant
// de proposition, ce qui coûte cher et conseils. Calculs côté serveur (app/analytics/budget.py).
import { cssVar, mountChart } from "../charts.js";
import { isMasked } from "../privacy.js";
import { el, emptyNote, formatMonth, hasTab, money, scopeParams, section } from "../ui.js";

export const id = "budget";
export const title = "Budget";

const STATUS = {
  ok: { text: "Dans les clous", color: "--success" },
  risk: { text: "À surveiller", color: "--warning" },
  over: { text: "Dépassé", color: "--danger" },
  none: { text: "Sans objectif", color: "--muted" },
};
const EFFORTS = [["light", "Léger (−5 % sur les postes variables)"], ["medium", "Moyen (−10 %)"],
                 ["strong", "Fort (−20 %)"]];
const TIP_LEVELS = { warn: ["À revoir", "warn"], good: ["Bien joué", "good"], info: ["À savoir", ""],
                     tip: ["Astuce", "auto"] };
const PCT0 = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 0 });

/** Montant saisi (« 350 », « 350,5 ») pour un champ : virgule décimale, sans zéros superflus. */
function inputValue(amount) {
  if (amount === null || amount === undefined) return "";
  return String(Number(amount)).replace(".", ",");
}

function plural(n, word, many = `${word}s`) {
  return `${n} ${n > 1 ? many : word}`;
}

function tile(label, value, note, tone = "") {
  return el("div", { className: "tile" },
    el("p", { className: "tile-label", text: label }),
    el("p", { className: `tile-value ${tone}`.trim(), text: value }),
    note ? el("p", { className: "variation", text: note }) : null);
}

function tiles(data) {
  const t = data.totals;
  if (t.target === null) return null;
  const items = [
    tile("Objectif de la période", money(t.target, "expense")),
    tile("Dépensé (catégories budgétées)", money(t.spent_budgeted, "expense"),
         `${PCT0.format(Number(t.spent_budgeted) / Math.max(Number(t.target), 0.01))} de l'objectif`),
  ];
  if (data.in_progress && t.expected !== null) {
    const ahead = Number(t.spent_budgeted) - Number(t.expected);
    items.push(tile("Attendu à ce jour", money(t.expected, "expense"),
      ahead > 0 ? `${money(ahead, "expense")} de plus que prévu à cette date` : "Dans le rythme prévu"));
  }
  if (data.in_progress && t.projection !== null) {
    const over = Number(t.projection) > Number(t.target);
    items.push(tile("Projection fin de période", money(t.projection, "expense"),
      over ? `${money(Number(t.projection) - Number(t.target), "expense")} au-delà de l'objectif`
        : "Sous l'objectif", over ? "neg" : ""));
  }
  if (t.per_day !== null) {
    const left = Number(t.per_day);
    items.push(tile("Reste à dépenser par jour", left >= 0 ? money(left, "expense") : "Plus rien",
      plural(data.days_left, "jour restant", "jours restants")));
  }
  if (Number(t.spent_unbudgeted) > 0) {
    items.push(tile("Hors budget", money(t.spent_unbudgeted, "expense"), "catégories sans objectif ou à classer"));
  }
  return el("div", { className: "tiles" }, items);
}

/** Lien vers la catégorie (onglet Catégories) ou vers les opérations à classer. */
function categoryLink(ctx, row) {
  if (row.id === null) return hasTab(ctx, "transactions") ? () => ctx.navigate("transactions", { category: "none" }) : null;
  return hasTab(ctx, "categories") ? () => ctx.navigate("categories", { id: String(row.id) }) : null;
}

function progress(row) {
  const target = Number(row.target);
  const spent = Number(row.spent);
  const scale = Math.max(target, spent, Number(row.projection || 0), 0.01);
  const track = el("span", { className: "budget-track" });
  const fill = el("span", { className: "budget-fill" });
  fill.style.width = `${Math.min(Math.max(spent, 0) / scale, 1) * 100}%`;
  fill.style.background = cssVar(STATUS[row.status].color);
  track.append(fill);
  if (row.projection !== null && Number(row.projection) > spent) {
    const ghost = el("span", { className: "budget-ghost", title: "Projection" });
    ghost.style.left = fill.style.width;
    ghost.style.width = `${(Math.min(Number(row.projection), scale) - Math.max(spent, 0)) / scale * 100}%`;
    track.append(ghost);
  }
  const marks = [[target, "budget-target", "Objectif"]];
  if (row.expected !== null) marks.push([Number(row.expected), "budget-expected", "Attendu à ce jour"]);
  for (const [value, className, label] of marks) {
    const mark = el("span", { className, title: label });
    mark.style.left = `${Math.min(value / scale, 1) * 100}%`;
    track.append(mark);
  }
  return track;
}

function budgetRow(ctx, row) {
  const status = STATUS[row.status];
  const open = categoryLink(ctx, row);
  const name = open ? el("button", { type: "button", className: "link budget-name", text: row.name,
                                     on: { click: open } })
    : el("span", { className: "budget-name", text: row.name });
  const details = [`${money(row.spent, "expense")} sur ${money(row.target, "expense")}`];
  if (row.expected !== null) details.push(`attendu à ce jour ${money(row.expected, "expense")}`);
  if (row.projection !== null) details.push(`projection ${money(row.projection, "expense")}`);
  return el("li", { className: "budget-row" },
    el("div", { className: "budget-head" }, name,
      el("span", { className: "budget-pct", text: row.percent === null ? "" : PCT0.format(row.percent) }),
      el("span", { className: `status budget-status ${row.status}`, text: status.text })),
    progress(row),
    el("p", { className: "muted budget-details", text: details.join(" · ") }));
}

function tracking(ctx, data) {
  const budgeted = data.rows.filter((r) => r.target !== null);
  const others = data.rows.filter((r) => r.target === null && Number(r.spent) !== 0);
  const order = { over: 0, risk: 1, ok: 2 };
  budgeted.sort((a, b) => order[a.status] - order[b.status] || Number(b.spent) - Number(a.spent));
  const parts = [];
  if (budgeted.length) {
    parts.push(el("ul", { className: "budget-list" }, budgeted.map((r) => budgetRow(ctx, r))));
    parts.push(el("p", { className: "muted legend", text: data.in_progress
      ? "Trait plein : objectif. Trait fin : attendu à ce jour d'après ton rythme habituel. Zone claire : projection."
      : "Trait plein : objectif de la période." }));
  } else {
    parts.push(emptyNote("Aucun objectif sur cette période. Lance l'assistant ou saisis tes objectifs."));
  }
  if (others.length) {
    parts.push(el("h3", { className: "budget-subtitle", text: "Sans objectif" }));
    parts.push(el("ul", { className: "plain-list" }, others.map((r) => {
      const open = categoryLink(ctx, r);
      return el("li", {},
        open ? el("button", { type: "button", className: "link label", text: r.name, on: { click: open } })
          : el("span", { className: "label", text: r.name }),
        el("span", { className: "amount", text: money(r.spent, "expense") }));
    })));
  }
  return parts;
}

function monthlyChart(host, allMonths) {
  const start = allMonths.findIndex((m) => m.target !== null || Number(m.spent_all) !== 0);
  const monthly = allMonths.slice(Math.max(start, 0));       // sans les mois vides du début
  mountChart(host, {
    data: {
      labels: monthly.map((m) => formatMonth(m.month)),
      datasets: [
        { type: "bar", label: "Dépensé (catégories budgétées)", order: 2,
          data: monthly.map((m) => Number(m.spent)), backgroundColor: cssVar("--chart-1") },
        { type: "line", label: "Objectif", order: 1, stepped: "middle",
          data: monthly.map((m) => (m.target === null ? null : Number(m.target))),
          borderColor: cssVar("--warning"), backgroundColor: cssVar("--warning"), pointRadius: 2 },
        { type: "line", label: "Toutes les dépenses", order: 0, borderDash: [4, 4], pointRadius: 0,
          data: monthly.map((m) => Number(m.spent_all)), borderColor: cssVar("--muted"),
          backgroundColor: cssVar("--muted") },
      ],
    },
    options: {
      scales: { y: { beginAtZero: true, ticks: { callback: (v) => money(v, "expense") } } },
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { label: (item) => `${item.dataset.label} : ${money(item.raw, "expense")}` } },
      },
    },
  });
}

// --- Modification des objectifs ------------------------------------------------------------------

function changesList(changes) {
  if (!changes.length) return emptyNote("Aucun objectif enregistré pour l'instant.");
  return el("div", { className: "scroll-x" }, el("table", { className: "data-table" },
    el("thead", {}, el("tr", {}, ["À partir de", "Catégorie", "Avant", "Après", "Modifié le"].map((h) =>
      el("th", { scope: "col", className: ["Avant", "Après"].includes(h) ? "num" : "", text: h })))),
    el("tbody", {}, changes.map((c) => el("tr", {},
      el("td", { text: formatMonth(c.month) }),
      el("td", { text: c.name }),
      el("td", { className: "num", text: c.previous === null ? "—" : money(c.previous, "expense") }),
      el("td", { className: "num", text: c.amount === null ? "retiré" : money(c.amount, "expense") }),
      el("td", { className: "muted", text: c.changed_at ? c.changed_at.slice(0, 10).split("-").reverse().join("/") : "" }))))));
}

/**
 * Formulaire de modification : un champ par catégorie, pré-rempli avec l'objectif du mois choisi (ou avec
 * `proposal` venant de l'assistant). `averages` : {id: moyenne mensuelle habituelle} pour repère.
 */
async function editor(ctx, slot, rerender, { proposal = null, averages = {} } = {}) {
  const data = await ctx.api.get("/tabs/budget/targets");
  const month = el("input", { type: "month", value: data.current_month, required: true,
                              attrs: { "aria-label": "Appliquer à partir du mois" } });
  const inputs = new Map();
  const body = el("tbody");
  const fill = (categories) => {
    body.replaceChildren(...categories.map((c) => {
      const value = proposal && c.id in proposal ? proposal[c.id] : c.amount;
      const input = el("input", { type: "text", inputMode: "decimal", value: inputValue(value), placeholder: "—",
                                  attrs: { "aria-label": `Objectif mensuel de ${c.name}` } });
      inputs.set(c.id, input);
      return el("tr", {},
        el("td", { text: c.name }),
        el("td", { className: "num muted",
                   text: c.amount === null ? "—" : `${money(c.amount, "expense")} depuis ${formatMonth(c.since)}` }),
        el("td", { className: "num muted",
                   text: Number(averages[c.id] || 0) > 0 ? money(averages[c.id], "expense") : "—" }),
        el("td", { className: "num" }, input, el("span", { className: "muted", text: " €" })));
    }));
  };
  fill(data.categories);
  month.addEventListener("change", async () => {
    if (!month.value) return;
    const current = await ctx.api.get("/tabs/budget/targets", { month: month.value });
    proposal = Object.fromEntries([...inputs].map(([cid, input]) => [cid, input.value]));
    fill(current.categories.map((c) => ({ ...c })));
    for (const [cid, input] of inputs) input.value = proposal[cid];
  });
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const save = el("button", { type: "submit", className: "primary", text: "Enregistrer" });
  const form = el("form", {},
    el("p", { className: "row-actions" }, el("label", { className: "check" }, "Appliquer à partir de ", month)),
    el("p", { className: "muted", text: "Les mois précédents gardent leurs objectifs ; un montant modifié vaut pour ce " +
      "mois et tous les suivants (il remplace les changements déjà prévus après). Laisse un champ vide pour ne pas " +
      "fixer d'objectif." }),
    el("div", { className: "scroll-x" }, el("table", { className: "data-table budget-edit" },
      el("thead", {}, el("tr", {}, ["Catégorie", "Objectif actuel", "Moyenne habituelle", "Objectif mensuel"].map((h, i) =>
        el("th", { scope: "col", className: i ? "num" : "", text: h })))),
      body)),
    el("div", { className: "actions" }, save,
      el("button", { type: "button", text: "Annuler", on: { click: () => slot.replaceChildren() } })),
    error);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    save.disabled = true;
    try {
      await ctx.api.put("/tabs/budget/targets", {
        from_month: month.value,
        targets: [...inputs].map(([cid, input]) => ({ category_id: cid, amount: input.value.trim() || null })),
      });
      slot.replaceChildren();
      await rerender();
    } catch (err) {
      error.textContent = `Enregistrement impossible : ${err.message}`;
      save.disabled = false;
    }
  });
  slot.replaceChildren(section("Modifier les objectifs", form,
    el("details", { className: "budget-history" }, el("summary", { text: "Historique des objectifs" }),
      changesList(data.changes))));
  slot.scrollIntoView({ behavior: "smooth", block: "start" });
}

// --- Assistant -----------------------------------------------------------------------------------

async function assistant(ctx, slot, rerender) {
  const accounts = ctx.accountIds.length ? ctx.accountIds.join(",") : undefined;
  const first = await ctx.api.get("/tabs/budget/suggest", { accounts });
  if (!first.history_months.length) {
    slot.replaceChildren(section("Proposer des objectifs",
      emptyNote("Il faut au moins un mois complet de dépenses avant ce mois-ci pour proposer des objectifs."),
      el("button", { type: "button", text: "Fermer", on: { click: () => slot.replaceChildren() } })));
    return;
  }
  const income = el("input", { type: "text", inputMode: "decimal", value: inputValue(first.income_average),
                               attrs: { "aria-label": "Revenu mensuel" } });
  const savings = el("input", { type: "text", inputMode: "decimal", value: "10",
                                attrs: { "aria-label": "Épargne visée" } });
  const unit = el("select", { attrs: { "aria-label": "Unité de l'épargne visée" } },
    el("option", { value: "pct", text: "% du revenu" }), el("option", { value: "eur", text: "€ par mois" }));
  const effort = el("select", { attrs: { "aria-label": "Effort" } },
    EFFORTS.map(([value, text]) => el("option", { value, text, selected: value === "medium" })));
  const result = el("div");
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const months = first.history_months;
  const form = el("form", { className: "budget-quiz" },
    el("p", { className: "muted", text: `D'après tes dépenses de ${formatMonth(months[0])} à ${formatMonth(months.at(-1))}` +
      ` (${plural(months.length, "mois", "mois")}). Les postes stables d'un mois sur l'autre (loyer, factures…) gardent leur montant ;` +
      " les postes variables baissent selon l'effort choisi." }),
    el("label", {}, el("span", { text: "Revenu mensuel (€)" }), income),
    el("label", {}, el("span", { text: "Épargne visée" }), el("span", { className: "row-actions" }, savings, unit)),
    el("label", {}, el("span", { text: "Effort" }), effort),
    el("div", { className: "actions" }, el("button", { type: "submit", className: "primary", text: "Proposer" }),
      el("button", { type: "button", text: "Fermer", on: { click: () => slot.replaceChildren() } })),
    error);

  const show = (s) => {
    const averages = Object.fromEntries(s.categories.map((c) => [c.id, c.average]));
    const proposal = Object.fromEntries(s.categories.map((c) => [c.id, c.suggested]));
    const rows = s.categories.filter((c) => Number(c.average) > 0 || c.suggested !== null);
    const notes = [];
    if (!s.feasible) {
      notes.push(el("p", { className: "tile-note", text: "Même en divisant par deux les postes variables, l'épargne visée " +
        "n'est pas atteignable : il faudra toucher aux postes stables, ou viser moins." }));
    }
    if (Number(s.unclassified_average) > 0) {
      notes.push(el("p", { className: "muted", text: `${money(s.unclassified_average, "expense")} par mois en moyenne ` +
        "restent à classer et ne sont pas budgétés : classe-les pour une proposition plus juste." }));
    }
    result.replaceChildren(
      el("div", { className: "tiles" },
        tile("Total des objectifs", money(s.total, "expense")),
        tile("Épargne possible", money(s.projected_savings, "savings"),
             Number(s.income) > 0 ? `${PCT0.format(Number(s.projected_savings) / Number(s.income))} du revenu` : "",
             Number(s.projected_savings) < Number(s.savings) ? "neg" : "")),
      ...notes,
      el("div", { className: "scroll-x" }, el("table", { className: "data-table" },
        el("thead", {}, el("tr", {}, ["Catégorie", "Type", "Moyenne", "Proposé"].map((h, i) =>
          el("th", { scope: "col", className: i > 1 ? "num" : "", text: h })))),
        el("tbody", {}, rows.map((c) => el("tr", {},
          el("td", { text: c.name }),
          el("td", {}, el("span", { className: `tag ${c.fixed ? "" : "auto"}`.trim(), text: c.fixed ? "stable" : "variable" })),
          el("td", { className: "num", text: money(c.average, "expense") }),
          el("td", { className: "num", text: c.suggested === null ? "—" : money(c.suggested, "expense") })))))),
      el("div", { className: "actions" }, el("button", { type: "button", className: "primary",
        text: "Utiliser ces montants…",
        on: { click: () => editor(ctx, slot, rerender, { proposal, averages }) } })));
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const revenue = Number(income.value.replace(",", ".").replace(/\s/g, ""));
    const target = Number(savings.value.replace(",", ".").replace(/\s/g, "") || 0);
    if (!Number.isFinite(revenue) || !Number.isFinite(target) || revenue < 0 || target < 0) {
      error.textContent = "Saisis des montants positifs, par exemple 2100 ou 2100,50.";
      return;
    }
    const savingsEur = unit.value === "pct" ? (revenue * target) / 100 : target;
    try {
      show(await ctx.api.get("/tabs/budget/suggest", { accounts, income: revenue.toFixed(2),
                                                        savings: savingsEur.toFixed(2), effort: effort.value }));
    } catch (err) {
      error.textContent = err.message;
    }
  });
  slot.replaceChildren(section("Proposer des objectifs", form, result));
  slot.scrollIntoView({ behavior: "smooth", block: "start" });
}

// --- Ce qui coûte cher et conseils ---------------------------------------------------------------

function insightsBlock(data) {
  const expenses = Number(data.expenses);
  const merchants = data.merchants.length
    ? el("ul", { className: "plain-list" }, data.merchants.map((m) => el("li", {},
      el("span", { className: "label", text: m.name, title: m.label }),
      el("span", { className: "muted", text: `${plural(m.count, "achat")}` +
        (expenses > 0 ? ` · ${PCT0.format(Number(m.total) / expenses)}` : "") }),
      el("span", { className: "amount", text: money(m.total, "expense") }))))
    : emptyNote("Aucune dépense sur la période.");
  const small = data.small_purchases;
  const smallNote = small.count
    ? el("p", { className: "muted", text: `Petits achats (< 15 €) : ${plural(small.count, "opération")} pour ` +
      `${money(small.total, "expense")}, soit ${money(small.per_month, "expense")} par mois.` })
    : null;
  const subscriptions = data.subscriptions.length
    ? [el("ul", { className: "plain-list" }, data.subscriptions.map((s) => el("li", {},
      el("span", { className: "label", text: s.name, title: s.label }),
      el("span", { className: "muted", text: `${money(s.monthly, "expense")} / mois` }),
      el("span", { className: "amount", text: `${money(s.yearly, "expense")} / an` })))),
       el("p", { className: "muted", text: `Total : ${money(data.subscriptions_yearly, "expense")} par an.` })]
    : [emptyNote("Aucune dépense récurrente détectée sur les 12 derniers mois.")];
  const tips = data.tips.length
    ? el("ul", { className: "plain-list budget-tips" }, data.tips.map((t) => {
      const [label, tone] = TIP_LEVELS[t.level] || TIP_LEVELS.info;
      return el("li", {}, el("span", { className: `tag ${tone}`.trim(), text: label }),
        el("span", { className: "label", text: isMasked("expense") ? maskEuros(t.text) : t.text }));
    }))
    : emptyNote("Rien à signaler sur cette période.");
  return [
    el("div", { className: "grid-2" },
      section("Ce qui coûte le plus cher", merchants, smallNote),
      section("Dépenses récurrentes (abonnements, loyer…)", ...subscriptions)),
    section("Conseils", tips),
  ];
}

/** Conseils en mode discret : les montants en euros du texte sont masqués comme ailleurs. */
function maskEuros(text) {
  return text.replace(/-?\d[\d\s ]*(?:[.,]\d+)?\s?€/g, "****** €");
}

export async function render(container, ctx) {
  const params = scopeParams(ctx);
  const [data, insights] = await Promise.all([
    ctx.api.get("/tabs/budget/summary", params),
    ctx.api.get("/tabs/budget/insights", params),
  ]);
  const slot = el("div", { className: "budget-slot" });
  const rerender = () => render(container, ctx);
  const wrap = (fn) => async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await fn();
    } catch (err) {
      slot.replaceChildren(el("p", { className: "error", text: err.message }));
    } finally {
      button.disabled = false;
    }
  };
  const actions = el("div", { className: "actions" },
    el("button", { type: "button", className: "primary", text: "Modifier les objectifs",
                   on: { click: wrap(() => editor(ctx, slot, rerender)) } }),
    el("button", { type: "button", text: "Proposer des objectifs (assistant)",
                   on: { click: wrap(() => assistant(ctx, slot, rerender)) } }));
  const chartHost = el("div", { className: "chart chart-wide" });
  const hasHistory = data.monthly.some((m) => m.target !== null);
  container.replaceChildren(...[
    tiles(data) || el("p", { className: "placeholder", text: "Fixe un montant mensuel par catégorie de dépense " +
      "pour suivre ton budget : saisis-le toi-même ou laisse l'assistant le proposer d'après tes dépenses." }),
    actions,
    slot,
    section("Suivi par catégorie", ...tracking(ctx, data)),
    hasHistory ? section("Objectif et dépenses, mois par mois", chartHost) : null,
    ...insightsBlock(insights)].filter(Boolean));
  if (hasHistory) monthlyChart(chartHost, data.monthly);
}
