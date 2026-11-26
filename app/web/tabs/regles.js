// Onglet « Règles » : tableau modifiable des règles de classement (motif, correspondance, sens, compte,
// catégorie, priorité), ajout, suppression et aperçu des libellés reconnus par un motif.
import { categorySelect, confirmDialog, el, emptyNote, score, section } from "../ui.js";

export const id = "regles";
export const title = "Règles";

const MATCHES = [
  ["contains", "contient"],
  ["startswith", "commence par"],
  ["regex", "expression régulière"],
];
const DIRECTIONS = [["", "débit ou crédit"], ["debit", "débit"], ["credit", "crédit"]];

function options(pairs, selected, label) {
  const select = el("select", { attrs: { "aria-label": label } },
    pairs.map(([value, text]) => el("option", { value, text })));
  select.value = selected ?? "";
  return select;
}

function accountSelect(accounts, selected) {
  return options([["", "tous les comptes"], ...accounts.map((a) => [String(a.id), `${a.label} ••${a.number_tail}`])],
    selected === null || selected === undefined ? "" : String(selected), "Compte");
}

/** Champs d'une règle (ligne du tableau ou formulaire d'ajout) ; `read()` renvoie le corps de la requête. */
function ruleFields(meta, rule = {}) {
  const fields = {
    pattern: el("input", { type: "text", value: rule.pattern || "", placeholder: "Motif",
                           attrs: { "aria-label": "Motif" } }),
    match: options(MATCHES, rule.match || "contains", "Type de correspondance"),
    direction: options(DIRECTIONS, rule.direction || "", "Sens"),
    account: accountSelect(meta.accounts, rule.account_id),
    category: categorySelect(meta.categories, rule.category_id ?? null, () => {}, "Catégorie"),
    priority: el("input", { type: "number", step: "1", value: rule.priority ?? "", placeholder: "auto",
                            attrs: { "aria-label": "Priorité" } }),
  };
  fields.category.querySelector('option[value=""]').textContent = "Catégorie…";
  const read = () => ({
    pattern: fields.pattern.value,
    match: fields.match.value,
    direction: fields.direction.value || null,
    account_id: fields.account.value ? Number(fields.account.value) : null,
    category_id: fields.category.value ? Number(fields.category.value) : null,
    priority: fields.priority.value === "" ? null : Number(fields.priority.value),
  });
  return { fields, read };
}

function previewBox() {
  const box = el("div", { className: "preview", attrs: { "aria-live": "polite" } });
  box.hidden = true;
  return box;
}

async function showPreview(ctx, box, body) {
  box.hidden = false;
  box.replaceChildren(el("p", { className: "muted", text: "Recherche…" }));
  try {
    const data = await ctx.api.post("/tabs/regles/test",
      { pattern: body.pattern, match: body.match, direction: body.direction });
    const more = data.total > data.labels.length ? ` (les ${data.labels.length} plus fréquents)` : "";
    box.replaceChildren(el("p", { className: "muted",
      text: `${data.total} libellé(s), ${data.tx_count} opération(s) reconnus${more}.` }));
    if (data.labels.length) {
      box.append(el("ul", { className: "plain-list" }, data.labels.map((l) => el("li", { text: l }))));
    }
  } catch (e) {
    box.replaceChildren(el("p", { className: "error", text: `Aperçu impossible : ${e.message}` }));
  }
}

function newRuleCard(meta, ctx) {
  const { fields, read } = ruleFields(meta);
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const box = previewBox();
  const previewButton = el("button", { type: "button", text: "Aperçu" });
  const add = el("button", { type: "button", className: "primary", text: "Ajouter la règle" });
  previewButton.addEventListener("click", () => showPreview(ctx, box, read()));
  add.addEventListener("click", async () => {
    const body = read();
    error.textContent = "";
    if (body.category_id === null) {
      error.textContent = "Choisissez une catégorie.";
      return;
    }
    add.disabled = true;
    try {
      await ctx.api.post("/tabs/regles", body);
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Ajout impossible : ${e.message}`;
      add.disabled = false;
    }
  });
  return section("Nouvelle règle",
    el("p", { className: "muted",
              text: "Une nouvelle règle classe les opérations encore à classer. La priorité la plus haute l'emporte ; " +
                    "laissée vide, elle dépend de la précision du motif." }),
    el("div", { className: "rule-form" }, fields.pattern, fields.match, fields.direction, fields.account,
       fields.category, fields.priority, previewButton, add),
    error, box);
}

function ruleRow(rule, meta, ctx) {
  const { fields, read } = ruleFields(meta, rule);
  const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });
  const box = previewBox();
  const save = el("button", { type: "button", className: "primary", text: "Enregistrer", disabled: true });
  const previewButton = el("button", { type: "button", text: "Aperçu" });
  const remove = el("button", { type: "button", className: "danger", text: "Supprimer" });

  for (const field of Object.values(fields)) {
    field.addEventListener("input", () => { save.disabled = false; });
    field.addEventListener("change", () => { save.disabled = false; });
  }
  previewButton.addEventListener("click", () => showPreview(ctx, box, read()));
  save.addEventListener("click", async () => {
    const body = read();
    error.textContent = "";
    if (body.category_id === null) {
      error.textContent = "Choisissez une catégorie.";
      return;
    }
    if (body.priority === null) delete body.priority;      // vide : on garde la priorité actuelle
    save.disabled = true;
    try {
      await ctx.api.patch(`/tabs/regles/${rule.id}`, body);
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Enregistrement impossible : ${e.message}`;
      save.disabled = false;
    }
  });
  remove.addEventListener("click", async () => {
    const question = `Supprimer la règle « ${rule.pattern} » → ${rule.category_path} ? ` +
      `Ses ${rule.tx_count} opération(s) seront reclassées par les autres règles ou iront dans « À classer ».`;
    if (!(await confirmDialog(question, { confirmLabel: "Supprimer", danger: true }))) return;
    remove.disabled = true;
    try {
      await ctx.api.delete(`/tabs/regles/${rule.id}`);
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Suppression impossible : ${e.message}`;
      remove.disabled = false;
    }
  });

  // `data-label` : intitulé de colonne affiché devant chaque champ quand le tableau passe en fiches (téléphone).
  const cell = (label, className, ...children) => el("td", { className, attrs: { "data-label": label } }, ...children);
  return el("tr", {},
    cell("Motif", "pattern", fields.pattern, box),
    cell("Correspondance", "", fields.match),
    cell("Sens", "", fields.direction),
    cell("Compte", "", fields.account),
    cell("Catégorie", "", fields.category),
    cell("Priorité", "num", fields.priority),
    el("td", { className: "num", attrs: { "data-label": "Opérations" }, text: String(rule.tx_count) }),
    el("td", { className: "actions" }, el("div", { className: "row-actions" }, previewButton, save, remove), error));
}

function rulesTable(rules, meta, ctx) {
  const head = el("thead", {}, el("tr", {},
    ["Motif", "Correspondance", "Sens", "Compte", "Catégorie", "Priorité", "Opérations", ""].map((h) =>
      el("th", { scope: "col", className: ["Priorité", "Opérations"].includes(h) ? "num" : "", text: h }))));
  const body = el("tbody", {}, rules.map((r) => ruleRow(r, meta, ctx)));
  return el("div", { className: "table-wrap scroll-x" }, el("table", { className: "data-table rules-table" }, head, body));
}

export async function render(container, ctx) {
  const [{ rules, auto }, meta] = await Promise.all([ctx.api.get("/tabs/regles"), ctx.api.get("/meta")]);
  container.replaceChildren(
    newRuleCard(meta, ctx),
    section(`Règles (${rules.length})`,
      el("p", { className: "muted",
                text: "Modifier ou supprimer une règle reclasse ses opérations, sauf celles classées à la main." }),
      el("p", { className: "muted auto-info",
                text: `${auto.count} opération${auto.count > 1 ? "s" : ""} classée${auto.count > 1 ? "s" : ""} ` +
                      `automatiquement (seuil ${score(auto.threshold)})` }),
      rules.length ? rulesTable(rules, meta, ctx) : emptyNote("Aucune règle.")));
}
