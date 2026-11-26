// Onglet « Catégories » : liste des catégories à gauche, détail à droite (sous-catégories, évolution
// mensuelle, transactions avec reclassement sur place). La catégorie affichée est le paramètre `id`.
// On y crée une racine (bas de la liste) ou une sous-catégorie, on renomme et on supprime (détail).
import { cssVar, mountChart } from "../charts.js";
import { confirmDialog, el, emptyNote, formatMonth, hasTab, money, scopeParams, section, setFill,
  transactionsTable } from "../ui.js";

export const id = "categories";
export const title = "Catégories";

const KIND_TITLES = { expense: "Dépenses", income: "Revenus", savings: "Épargne" };
const NAME_MAX = 60;

function plural(n, word, many = `${word}s`) {
  return `${n} ${n > 1 ? many : word}`;
}

/**
 * Petit formulaire (nom + champs éventuels) : `submit(name)` envoie la requête ; une erreur s'affiche
 * sous le formulaire sans le fermer. Entrée valide, Échap annule.
 */
function nameForm({ label, value = "", submitText, extra = [], submit, cancel }) {
  const input = el("input", { type: "text", value, maxLength: NAME_MAX, required: true,
                              placeholder: label, attrs: { "aria-label": label } });
  const ok = el("button", { type: "submit", className: "primary", text: submitText });
  const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });
  const form = el("form", { className: "cat-form" },
    el("div", { className: "row-actions" }, input, ...extra, ok,
       el("button", { type: "button", text: "Annuler", on: { click: cancel } })),
    error);
  form.addEventListener("keydown", (e) => { if (e.key === "Escape") cancel(); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    ok.disabled = true;
    try {
      await submit(input.value.trim());
    } catch (err) {
      error.textContent = err.message;
      ok.disabled = false;
    }
  });
  queueMicrotask(() => { input.focus(); input.select(); });
  return form;
}

function newRootBlock(ctx) {
  const block = el("div", { className: "cat-new" });
  const close = () => block.replaceChildren(el("button", {
    type: "button", className: "link", text: "+ Nouvelle catégorie", on: { click: open } }));
  const open = () => {
    const kind = el("select", { attrs: { "aria-label": "Type" } },
      Object.entries(KIND_TITLES).map(([value, text]) => el("option", { value, text })));
    block.replaceChildren(nameForm({
      label: "Nom de la catégorie", submitText: "Créer", extra: [kind], cancel: close,
      submit: async (name) => {
        const created = await ctx.api.post("/tabs/categories", { name, kind: kind.value });
        ctx.navigate("categories", { id: String(created.id) });
      },
    }));
  };
  close();
  return block;
}

function categoryButton(ctx, category, selectedId, className, kind) {
  if (category.id === null) {         // pseudo-ligne (virements internes non classés) : pas de détail
    return el("div", { className: `cat-link static ${className}` },
      el("span", { text: category.name }), el("span", { className: "muted", text: money(category.amount, kind) }));
  }
  const current = category.id === selectedId;
  return el("button", {
    type: "button",
    className: `cat-link ${className}`,
    attrs: { "aria-current": current ? "true" : "false" },
    on: { click: () => ctx.navigate("categories", { id: category.id }) },
  }, el("span", { text: category.name }), el("span", { className: "muted", text: money(category.amount, kind) }));
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
      const item = el("li", {}, categoryButton(ctx, root, selectedId, "root", kind));
      const showChildren = root.id === selectedId || root.children.some((c) => c.id === selectedId);
      const children = root.children.filter((c) => showChildren || c.id === null);   // pseudo-ligne toujours visible
      if (children.length) {
        item.append(el("ul", {}, children.map((c) => el("li", {}, categoryButton(ctx, c, selectedId, "child", kind)))));
      }
      list.append(item);
    }
    nav.append(list);
  }
  const { debits, credits, count } = tree.unclassified;
  // `count` inclut les virements internes appariés non classés (comptés en épargne, pas en débits/crédits)
  if ((Number(debits) || Number(credits) || count) && hasTab(ctx, "transactions")) {
    nav.append(el("h3", { text: "À classer" }), el("button", {
      type: "button",
      className: "cat-link root",
      on: { click: () => ctx.navigate("transactions", { category: "none" }) },
    }, el("span", { text: "Opérations à classer" }),
    el("span", { className: "muted", text: Number(debits) || Number(credits)
      ? `${money(debits, "expense")} / ${money(credits, "income")}`
      : `${count} opération${count > 1 ? "s" : ""}` })));
  }
  nav.append(newRootBlock(ctx));
  return nav;
}

function defaultCategory(tree) {
  const expenses = tree.roots.filter((r) => r.kind === "expense" && Number(r.amount) > 0)
    .sort((a, b) => Number(b.amount) - Number(a.amount));
  return (expenses[0] || tree.roots.find((r) => r.id !== null))?.id ?? null;
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
  header.append(el("h2", { text: data.name }), el("p", { className: "tile-value", text: money(data.amount, data.kind) }),
    manageBar(ctx, data, meta));
  panel.append(header);

  if (data.children.length) {
    const max = Math.max(...data.children.map((c) => Number(c.amount)), 0);
    panel.append(section("Sous-catégories", el("ul", { className: "bars" }, data.children.map((c) => {
      const fill = setFill(el("span", { className: "bar-fill" }), max > 0 ? Number(c.amount) / max : 0);
      const isSelf = c.id === data.id || c.id === null;     // pseudo-ligne : pas de détail
      return el("li", {}, el("button", {
        type: "button", className: "bar-row-button", disabled: isSelf,
        on: { click: () => ctx.navigate("categories", { id: c.id }) },
      }, el("span", { className: "bar-name", text: c.name }), el("span", { className: "bar-track" }, fill),
      el("span", { className: "bar-amount", text: money(c.amount, data.kind) })));
    }))));
  }

  const chartHost = el("div", { className: "chart" });
  panel.append(section("Évolution mensuelle", chartHost));

  const reclass = async (tx, newCategoryId) => {
    await ctx.api.post(`/tabs/categories/transactions/${tx.id}/category`, { category_id: newCategoryId });
    await ctx.refresh();
  };
  const shown = data.transactions.length;
  const txSection = section(shown < data.transactions_total
    ? `Transactions (${shown} sur ${data.transactions_total}, les plus récentes)`
    : `Transactions (${shown})`,
  shown ? transactionsTable(data.transactions, { categories: meta.categories, accounts: meta.accounts, reclass })
    : emptyNote("Aucune transaction sur la période."));
  if (shown < data.transactions_total && hasTab(ctx, "transactions")) {
    txSection.append(el("button", {
      type: "button", className: "link", text: "Voir toutes les transactions de cette catégorie",
      on: { click: () => ctx.navigate("transactions", { category: String(data.id) }) },
    }));
  }
  panel.append(txSection);

  return { panel, draw: () => mountChart(chartHost, {
    type: "bar",
    data: {
      labels: data.monthly.map((m) => formatMonth(m.month)),
      datasets: [{ label: data.name, data: data.monthly.map((m) => Number(m.amount)),
                   backgroundColor: cssVar("--accent") }],
    },
    options: {
      scales: { y: { ticks: { callback: (v) => money(v, data.kind) } } },
      plugins: { legend: { display: false },
                 tooltip: { callbacks: { label: (item) => money(item.raw, data.kind) } } },
    },
  }) };
}

/** Catégories vers lesquelles déplacer les usages d'une catégorie supprimée : même type, elle exclue. */
function destinationSelect(meta, data) {
  const select = el("select", { attrs: { "aria-label": "Catégorie de destination" } });
  for (const root of meta.categories.filter((r) => r.kind === data.kind)) {
    const group = el("optgroup", { label: root.name });
    if (root.id !== data.id) group.append(el("option", { value: String(root.id), text: root.name }));
    for (const child of root.children.filter((c) => c.id !== data.id)) {
      group.append(el("option", { value: String(child.id), text: `${root.name} › ${child.name}` }));
    }
    if (group.children.length) select.append(group);
  }
  if (data.parent_id !== null) select.value = String(data.parent_id);
  return select;
}

/** Boutons Renommer / Sous-catégorie / Supprimer et zone du formulaire ouvert (un seul à la fois). */
function manageBar(ctx, data, meta) {
  const slot = el("div", { className: "cat-slot" });
  const close = () => slot.replaceChildren();
  const showError = (text) => slot.replaceChildren(el("p", { className: "error", attrs: { role: "alert" }, text }));
  const afterDelete = () => ctx.navigate("categories", data.parent_id === null ? {} : { id: String(data.parent_id) });

  const rename = () => slot.replaceChildren(nameForm({
    label: "Nouveau nom", value: data.name, submitText: "Renommer", cancel: close,
    submit: async (name) => {
      await ctx.api.patch(`/tabs/categories/${data.id}`, { name });
      await ctx.refresh();
    },
  }));

  const addChild = () => slot.replaceChildren(nameForm({
    label: "Nom de la sous-catégorie", submitText: "Ajouter", cancel: close,
    submit: async (name) => {
      const created = await ctx.api.post("/tabs/categories", { name, parent_id: data.id });
      ctx.navigate("categories", { id: String(created.id) });
    },
  }));

  const remove = async () => {
    if (data.subcategories) {
      showError(`« ${data.name} » a encore ${plural(data.subcategories, "sous-catégorie")} : supprimez-les d'abord.`);
      return;
    }
    const { transactions, rules, overrides } = data.usage;
    if (!transactions && !rules && !overrides) {
      if (!(await confirmDialog(`Supprimer la catégorie « ${data.name} » ? Aucune opération ni règle ne l'utilise.`,
                                { confirmLabel: "Supprimer", danger: true }))) return;
      try {
        await ctx.api.delete(`/tabs/categories/${data.id}`);
        afterDelete();
      } catch (e) {
        showError(`Suppression impossible : ${e.message}`);
      }
      return;
    }
    const used = [plural(transactions, "opération"), plural(rules, "règle"),
                  plural(overrides, "reclassement manuel", "reclassements manuels")].join(", ");
    const target = destinationSelect(meta, data);
    if (!target.options.length) {
      showError(`« ${data.name} » est utilisée (${used}) et aucune autre catégorie de type ` +
                `${KIND_TITLES[data.kind].toLowerCase()} ne peut les recevoir : créez-en une d'abord.`);
      return;
    }
    const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });
    const confirm = el("button", { type: "button", className: "danger", text: "Supprimer et déplacer" });
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      error.textContent = "";
      try {
        await ctx.api.delete(`/tabs/categories/${data.id}?move_to=${encodeURIComponent(target.value)}`);
        afterDelete();
      } catch (e) {
        error.textContent = `Suppression impossible : ${e.message}`;
        confirm.disabled = false;
      }
    });
    slot.replaceChildren(el("div", { className: "cat-form" },
      el("p", { text: `« ${data.name} » est utilisée (${used}). Pour la supprimer, tout sera déplacé vers :` }),
      el("div", { className: "row-actions" }, target, confirm,
         el("button", { type: "button", text: "Annuler", on: { click: close } })),
      error));
  };

  const buttons = [el("button", { type: "button", text: "Renommer", on: { click: rename } })];
  if (data.parent_id === null) {
    buttons.push(el("button", { type: "button", text: "+ Sous-catégorie", on: { click: addChild } }));
  }
  buttons.push(el("button", { type: "button", className: "danger", text: "Supprimer", on: { click: remove } }));
  return el("div", {}, el("div", { className: "row-actions cat-actions" }, buttons), slot);
}

export async function render(container, ctx) {
  const [tree, meta] = await Promise.all([
    ctx.api.get("/tabs/categories/tree", scopeParams(ctx)),
    ctx.api.get("/meta"),
  ]);
  const requested = /^\d+$/.test(ctx.params.id || "") ? Number(ctx.params.id) : null;
  const known = new Set(tree.roots.flatMap((r) => [r.id, ...r.children.map((c) => c.id)]));
  const selectedId = requested !== null && known.has(requested) ? requested : defaultCategory(tree);

  if (selectedId === null) {
    container.replaceChildren(emptyNote("Aucune catégorie définie."), newRootBlock(ctx));
    return;
  }
  const { panel, draw } = await detailPanel(ctx, selectedId, meta);
  container.replaceChildren(el("div", { className: "split" }, sidebar(ctx, tree, selectedId), panel));
  draw();
}
