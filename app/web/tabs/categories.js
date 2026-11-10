// Onglet « Catégories » : liste des catégories à gauche, détail à droite (sous-catégories, évolution
// mensuelle, transactions avec reclassement sur place). La catégorie affichée est le paramètre `id`.
import { cssVar, mountChart } from "../charts.js";
import { el, emptyNote, formatMonth, money, scopeParams, section, transactionsTable } from "../ui.js";

export const id = "categories";
export const title = "Catégories";

const KIND_TITLES = { expense: "Dépenses", income: "Revenus", savings: "Épargne" };

function categoryButton(ctx, category, selectedId, className) {
  const current = category.id === selectedId;
  return el("button", {
    type: "button",
    className: `cat-link ${className}`,
    attrs: { "aria-current": current ? "true" : "false" },
    on: { click: () => ctx.navigate("categories", { id: category.id }) },
  }, el("span", { text: category.name }), el("span", { className: "muted", text: money(category.amount) }));
}

function sidebar(ctx, tree, selectedId) {
  const nav = el("nav", { className: "cat-list", attrs: { "aria-label": "Catégories" } });
  for (const kind of Object.keys(KIND_TITLES)) {
    const roots = tree.roots.filter((r) => r.kind === kind)
      .sort((a, b) => Number(b.amount) - Number(a.amount) || a.name.localeCompare(b.name, "fr"));
    if (!roots.length) continue;
    nav.append(el("h3", { text: KIND_TITLES[kind] }));
    const list = el("ul");
    for (const root of roots) {
      const item = el("li", {}, categoryButton(ctx, root, selectedId, "root"));
      const showChildren = root.id === selectedId || root.children.some((c) => c.id === selectedId);
      if (showChildren && root.children.length) {
        item.append(el("ul", {}, root.children.map((c) => el("li", {}, categoryButton(ctx, c, selectedId, "child")))));
      }
      list.append(item);
    }
    nav.append(list);
  }
  const { debits, credits } = tree.unclassified;
  if (Number(debits) || Number(credits)) {
    nav.append(el("h3", { text: "À classer" }), el("button", {
      type: "button",
      className: "cat-link root",
      on: { click: () => ctx.navigate("transactions", { category: "none" }) },
    }, el("span", { text: "Opérations à classer" }),
    el("span", { className: "muted", text: `${money(debits)} / ${money(credits)}` })));
  }
  return nav;
}

function defaultCategory(tree) {
  const expenses = tree.roots.filter((r) => r.kind === "expense" && Number(r.amount) > 0)
    .sort((a, b) => Number(b.amount) - Number(a.amount));
  return (expenses[0] || tree.roots[0])?.id ?? null;
}

async function detailPanel(ctx, categoryId, meta) {
  const data = await ctx.api.get(`/tabs/categories/${categoryId}`, scopeParams(ctx));
  const panel = el("div", { className: "cat-detail" });

  const header = el("div", { className: "cat-header" });
  if (data.parent_id !== null) {
    const parent = meta.categories.find((c) => c.id === data.parent_id);
    header.append(el("button", {
      type: "button", className: "link",
      text: `← ${parent ? parent.name : "Catégorie parente"}`,
      on: { click: () => ctx.navigate("categories", { id: data.parent_id }) },
    }));
  }
  header.append(el("h2", { text: data.name }), el("p", { className: "tile-value", text: money(data.amount) }));
  panel.append(header);

  if (data.children.length) {
    const max = Math.max(...data.children.map((c) => Number(c.amount)), 0);
    panel.append(section("Sous-catégories", el("ul", { className: "bars" }, data.children.map((c) => {
      const fill = el("span", { className: "bar-fill" });
      fill.style.width = `${max > 0 ? (Math.max(Number(c.amount), 0) / max) * 100 : 0}%`;
      fill.style.background = cssVar("--accent");
      const isSelf = c.id === data.id;
      return el("li", {}, el("button", {
        type: "button", className: "bar-row-button", disabled: isSelf,
        on: { click: () => ctx.navigate("categories", { id: c.id }) },
      }, el("span", { className: "bar-name", text: c.name }), el("span", { className: "bar-track" }, fill),
      el("span", { className: "bar-amount", text: money(c.amount) })));
    }))));
  }

  const chartHost = el("div", { className: "chart" });
  panel.append(section("Évolution mensuelle", chartHost));

  const reclass = async (tx, newCategoryId) => {
    await ctx.api.post(`/tabs/categories/transactions/${tx.id}/category`, { category_id: newCategoryId });
    await ctx.refresh();
  };
  panel.append(section(`Transactions (${data.transactions.length})`, data.transactions.length
    ? transactionsTable(data.transactions, { categories: meta.categories, accounts: meta.accounts, reclass })
    : emptyNote("Aucune transaction sur la période.")));

  return { panel, draw: () => mountChart(chartHost, {
    type: "bar",
    data: {
      labels: data.monthly.map((m) => formatMonth(m.month)),
      datasets: [{ label: data.name, data: data.monthly.map((m) => Number(m.amount)),
                   backgroundColor: cssVar("--accent") }],
    },
    options: {
      scales: { y: { ticks: { callback: (v) => money(v) } } },
      plugins: { legend: { display: false },
                 tooltip: { callbacks: { label: (item) => money(item.raw) } } },
    },
  }) };
}

export async function render(container, ctx) {
  const [tree, meta] = await Promise.all([
    ctx.api.get("/tabs/categories/tree", scopeParams(ctx)),
    ctx.api.get("/meta"),
  ]);
  const requested = /^\d+$/.test(ctx.params.id || "") ? Number(ctx.params.id) : null;
  const known = new Set(tree.roots.flatMap((r) => [r.id, ...r.children.map((c) => c.id)]));
  const selectedId = known.has(requested) ? requested : defaultCategory(tree);

  if (selectedId === null) {
    container.replaceChildren(emptyNote("Aucune catégorie définie."));
    return;
  }
  const { panel, draw } = await detailPanel(ctx, selectedId, meta);
  container.replaceChildren(el("div", { className: "split" }, sidebar(ctx, tree, selectedId), panel));
  draw();
}
