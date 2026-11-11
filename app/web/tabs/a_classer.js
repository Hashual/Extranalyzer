// Onglet « À classer » : une ligne par libellé normalisé sans catégorie (période et comptes de la barre),
// avec catégorie → sous-catégorie pré-remplies par la suggestion du modèle (et son score), création d'une
// règle (cochée par défaut, sauf sur une suggestion), et validation groupée de ce qui est affiché.
import { amount, el, emptyNote, formatDate, scopeParams, score } from "../ui.js";

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
  /** Sélectionne une catégorie racine ou une sous-catégorie (id inconnu : rien). */
  const select = (categoryId) => {
    const parent = categories.find((c) => c.id === categoryId || c.children.some((k) => k.id === categoryId));
    if (!parent) return;
    root.value = String(parent.id);
    fillChildren();
    if (parent.id !== categoryId) child.value = String(categoryId);
  };
  return {
    nodes: [root, child],
    select,
    value: () => (child.value || root.value ? Number(child.value || root.value) : null),
    onChange: (fn) => [root, child].forEach((s) => s.addEventListener("change", fn)),
  };
}

/** Ligne d'un groupe ; renvoie { node, group, picker } (le bouton de validation groupée lit `picker`). */
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
  let suggestion = null;
  if (group.suggested_category_id !== null) {
    picker.select(group.suggested_category_id);
    submit.disabled = picker.value() === null;
    createRule.checked = false;         // pas de règle d'un clic à partir d'une proposition du modèle
    suggestion = el("span", { className: "tag suggestion", text: `suggestion ${score(group.confidence)}`,
                              title: group.reason || "" });
  }
  ruleFields.hidden = !createRule.checked;
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
  const node = el("li", { className: "group-row" },
    el("div", { className: "group-head" },
      el("div", { className: "group-label" },
        el("strong", { text: group.sample_label }),
        el("span", { className: "muted", text: `${group.count} opération(s), ${SIGNS[group.sign]} · ${dates}` })),
      amount(group.total)),
    el("div", { className: "group-actions" },
      ...picker.nodes,
      suggestion,
      el("label", { className: "check" }, createRule, " Créer une règle"),
      ruleFields,
      submit),
    error);
  return { node, group, picker };
}

/**
 * « Valider les suggestions affichées (N) » : applique ce que l'écran montre — la catégorie choisie dans chaque
 * groupe (pré-remplie ou modifiée) devient une décision manuelle pour toutes ses opérations, sans règle.
 */
function acceptAll(rows, ctx) {
  const chosen = () => rows.filter((r) => r.picker.value() !== null);
  const button = el("button", { type: "button", className: "primary" });
  let n = 0;
  const update = () => {
    n = chosen().reduce((total, r) => total + r.group.count, 0);
    button.textContent = `Valider les suggestions affichées (${n})`;
    button.disabled = n === 0;
  };
  rows.forEach((r) => r.picker.onChange(update));
  update();
  const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });
  button.addEventListener("click", async () => {
    const items = chosen().map((r) => ({ label_norm: r.group.label_norm, category_id: r.picker.value() }));
    if (!items.length) return;
    const question = `Classer ${n} opération(s) dans les catégories affichées ? ` +
                     "Chacune devient une décision manuelle ; aucune règle n'est créée.";
    if (!window.confirm(question)) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const query = new URLSearchParams(scopeParams(ctx)).toString();
      await ctx.api.post(`/tabs/a_classer/accept_suggestions?${query}`, { items });
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Validation impossible : ${e.message}`;
      update();
    }
  });
  return el("div", { className: "accept-all" }, button, error);
}

export async function render(container, ctx) {
  const [data, meta] = await Promise.all([
    ctx.api.get("/tabs/a_classer/list", scopeParams(ctx)), ctx.api.get("/meta")]);
  if (!data.groups.length) {
    container.replaceChildren(emptyNote("Aucune opération à classer sur cette période."));
    return;
  }
  const count = data.groups.reduce((n, g) => n + g.count, 0);
  const rows = data.groups.map((g) => groupRow(g, meta.categories, ctx));
  container.replaceChildren(
    el("p", { className: "muted",
              text: `${count} opération(s) à classer sur la période, en ${data.groups.length} libellé(s). ` +
                    "Une règle classe aussi les opérations à classer des autres périodes." }),
    acceptAll(rows, ctx),
    el("ul", { className: "group-list" }, rows.map((r) => r.node)));
}
