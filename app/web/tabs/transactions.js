// Onglet « Transactions » : tableau filtrable (texte, catégorie), triable et paginé, avec reclassement.
// Les filtres vivent dans les paramètres de l'onglet (hash de l'URL) : q, category, sort, page.
import { el, emptyNote, scopeParams, transactionsTable } from "../ui.js";

export const id = "transactions";
export const title = "Transactions";

const SORTS = [
  ["date_desc", "Date (récentes d'abord)"],
  ["date_asc", "Date (anciennes d'abord)"],
  ["amount_asc", "Montant croissant"],
  ["amount_desc", "Montant décroissant"],
];

function filters(ctx, meta, current) {
  const apply = (changes) => {
    const next = { ...current, ...changes };
    if (!("page" in changes)) next.page = "1";
    const params = Object.fromEntries(Object.entries(next).filter(([, v]) => v !== "" && v !== undefined));
    if (params.sort === "date_desc") delete params.sort;
    if (params.page === "1") delete params.page;
    ctx.navigate("transactions", params);
  };

  const search = el("input", { type: "search", value: current.q || "", placeholder: "Rechercher un libellé",
                               attrs: { "aria-label": "Rechercher un libellé" } });
  search.addEventListener("change", () => apply({ q: search.value.trim() }));

  const category = el("select", { attrs: { "aria-label": "Filtrer par catégorie" } },
    el("option", { value: "", text: "Toutes les catégories" }),
    el("option", { value: "none", text: "À classer" }));
  for (const root of meta.categories) {
    const group = el("optgroup", { label: root.name });
    group.append(el("option", { value: String(root.id), text: `${root.name} (tout)` }));
    for (const child of root.children) group.append(el("option", { value: String(child.id), text: child.name }));
    category.append(group);
  }
  category.value = current.category || "";
  category.addEventListener("change", () => apply({ category: category.value }));

  const sort = el("select", { attrs: { "aria-label": "Trier" } },
    SORTS.map(([value, text]) => el("option", { value, text })));
  sort.value = current.sort || "date_desc";
  sort.addEventListener("change", () => apply({ sort: sort.value }));

  return { node: el("div", { className: "filters" }, search, category, sort), apply };
}

function pager(data, apply) {
  const prev = el("button", { type: "button", text: "← Précédente", disabled: data.page <= 1,
                              on: { click: () => apply({ page: String(data.page - 1) }) } });
  const next = el("button", { type: "button", text: "Suivante →", disabled: data.page >= data.pages,
                              on: { click: () => apply({ page: String(data.page + 1) }) } });
  return el("div", { className: "pager" }, prev, el("span", { text: `Page ${data.page} / ${data.pages}` }), next);
}

export async function render(container, ctx) {
  const current = {
    q: ctx.params.q || "",
    category: ctx.params.category || "",
    sort: ctx.params.sort || "date_desc",
    page: ctx.params.page || "1",
  };
  const params = { ...scopeParams(ctx), sort: current.sort, page: /^\d+$/.test(current.page) ? current.page : "1" };
  if (current.q) params.q = current.q;
  if (current.category) params.category = current.category;

  const [data, meta] = await Promise.all([ctx.api.get("/tabs/transactions/list", params), ctx.api.get("/meta")]);
  const { node, apply } = filters(ctx, meta, current);
  const reclass = async (tx, categoryId) => {
    await ctx.api.post(`/tabs/transactions/${tx.id}/category`, { category_id: categoryId });
    await ctx.refresh();
  };

  container.replaceChildren(
    node,
    el("p", { className: "muted", text: `${data.total} opération(s)` }),
    data.items.length
      ? transactionsTable(data.items, { categories: meta.categories, accounts: meta.accounts, reclass })
      : emptyNote("Aucune opération ne correspond."),
    data.pages > 1 ? pager(data, apply) : "");
}
