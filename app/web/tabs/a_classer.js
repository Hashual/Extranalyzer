// Onglet « À classer » : une ligne par libellé normalisé sans catégorie (période et comptes de la barre),
// avec catégorie → sous-catégorie, et création d'une règle (cochée par défaut, motif modifiable).
import { amount, el, emptyNote, formatDate, scopeParams } from "../ui.js";

export const id = "a_classer";
export const title = "À classer";

const MATCHES = [
  ["contains", "contient"],
  ["startswith", "commence par"],
  ["regex", "expression régulière"],
];
const SIGNS = { debit: "débits", credit: "crédits", mixed: "débits et crédits" };

function categoryPicker(categories) {
  const root = el("select", { attrs: { "aria-label": "Catégorie" } },
    el("option", { value: "", text: "Catégorie…" }),
    categories.map((c) => el("option", { value: String(c.id), text: c.name })));
  const child = el("select", { disabled: true, attrs: { "aria-label": "Sous-catégorie" } });
  const fillChildren = () => {
    const parent = categories.find((c) => String(c.id) === root.value);
    child.replaceChildren(el("option", { value: "", text: parent ? "(sans sous-catégorie)" : "Sous-catégorie…" }),
      ...(parent?.children || []).map((c) => el("option", { value: String(c.id), text: c.name })));
    child.disabled = !parent || !parent.children.length;
  };
  root.addEventListener("change", fillChildren);
  fillChildren();
  return {
    nodes: [root, child],
    value: () => (child.value || root.value ? Number(child.value || root.value) : null),
    onChange: (fn) => [root, child].forEach((s) => s.addEventListener("change", fn)),
  };
}

function groupRow(group, categories, ctx) {
  const picker = categoryPicker(categories);
  const createRule = el("input", { type: "checkbox", checked: true });
  const pattern = el("input", { type: "text", value: group.label_norm,
                                attrs: { "aria-label": `Motif de la règle pour « ${group.sample_label} »` } });
  const match = el("select", { attrs: { "aria-label": "Type de correspondance" } },
    MATCHES.map(([value, text]) => el("option", { value, text })));
  const ruleFields = el("div", { className: "rule-fields" }, match, pattern);
  const submit = el("button", { type: "button", className: "primary", text: "Classer", disabled: true });
  const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });

  picker.onChange(() => { submit.disabled = picker.value() === null; });
  createRule.addEventListener("change", () => { ruleFields.hidden = !createRule.checked; });

  submit.addEventListener("click", async () => {
    const body = { label_norm: group.label_norm, category_id: picker.value(), create_rule: createRule.checked };
    if (createRule.checked) Object.assign(body, { pattern: pattern.value, match: match.value });
    submit.disabled = true;
    error.textContent = "";
    try {
      const query = new URLSearchParams(scopeParams(ctx)).toString();
      await ctx.api.post(`/tabs/a_classer/classify?${query}`, body);
      await ctx.refresh();          // la ligne disparaît, la pastille de la barre se met à jour
    } catch (e) {
      error.textContent = `Classement impossible : ${e.message}`;
      submit.disabled = false;
    }
  });

  const dates = group.first_date === group.last_date
    ? formatDate(group.first_date)
    : `${formatDate(group.first_date)} → ${formatDate(group.last_date)}`;
  return el("li", { className: "group-row" },
    el("div", { className: "group-head" },
      el("div", { className: "group-label" },
        el("strong", { text: group.sample_label }),
        el("span", { className: "muted", text: `${group.count} opération(s), ${SIGNS[group.sign]} · ${dates}` })),
      amount(group.total)),
    el("div", { className: "group-actions" },
      ...picker.nodes,
      el("label", { className: "check" }, createRule, " Créer une règle"),
      ruleFields,
      submit),
    error);
}

export async function render(container, ctx) {
  const [data, meta] = await Promise.all([
    ctx.api.get("/tabs/a_classer/list", scopeParams(ctx)), ctx.api.get("/meta")]);
  if (!data.groups.length) {
    container.replaceChildren(emptyNote("Aucune opération à classer sur cette période."));
    return;
  }
  const count = data.groups.reduce((n, g) => n + g.count, 0);
  container.replaceChildren(
    el("p", { className: "muted",
              text: `${count} opération(s) à classer sur la période, en ${data.groups.length} libellé(s). ` +
                    "Une règle classe aussi les opérations à classer des autres périodes." }),
    el("ul", { className: "group-list" }, data.groups.map((g) => groupRow(g, meta.categories, ctx))));
}
