// Petits utilitaires d'affichage partagés par les onglets.
// Tout texte venant du serveur passe par textContent (les libellés viennent des relevés bancaires).

const EUR = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const PCT = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 1, signDisplay: "exceptZero" });
const SCORE = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 0 });
const MONTH = new Intl.DateTimeFormat("fr-FR", { month: "short", year: "numeric", timeZone: "UTC" });

/** Montant (chaîne "123.45" de l'API) → « 123,45 € ». */
export function money(value) {
  return EUR.format(Number(value));
}

export function percent(ratio) {
  return PCT.format(ratio);
}

/** Confiance du modèle (0..1) → « 94 % ». */
export function score(confidence) {
  return SCORE.format(confidence);
}

/** Confiance d'une suggestion, tronquée (0,896 → « 89 % », jamais « 90 % » sous le seuil). */
export function suggestionScore(confidence) {
  return SCORE.format(Math.floor(confidence * 100 + 1e-9) / 100);   // 1e-9 : bruit flottant (0,29 × 100)
}

/** "2025-01-31" → "31/01/2025". */
export function formatDate(isoDate) {
  const [y, m, d] = isoDate.split("-");
  return `${d}/${m}/${y}`;
}

/** "2025-01" → « janv. 2025 ». */
export function formatMonth(key) {
  const [y, m] = key.split("-").map(Number);
  return MONTH.format(new Date(Date.UTC(y, m - 1, 1)));
}

/**
 * Crée un élément : el("p", { className: "x", text: "…", dataset: {…}, on: { click: fn } }, ...enfants).
 * Les enfants chaînes deviennent des nœuds texte.
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  const { text, dataset, on, attrs, ...rest } = props;
  Object.assign(node, rest);
  if (text !== undefined && text !== null) node.textContent = text;
  for (const [key, value] of Object.entries(dataset || {})) node.dataset[key] = value;
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  for (const [event, handler] of Object.entries(on || {})) node.addEventListener(event, handler);
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

/** Montant coloré : négatif en rouge, positif en vert (classes CSS). */
export function amount(value, tag = "span") {
  const n = Number(value);
  return el(tag, { className: `amount ${n < 0 ? "neg" : n > 0 ? "pos" : ""}`.trim(), text: money(value) });
}

/** Vrai si l'onglet `tabId` est activé (les liens vers un onglet désactivé sont masqués). */
export function hasTab(ctx, tabId) {
  return ctx.enabledTabs.includes(tabId);
}

/** Paramètres de requête communs (période, comparaison, comptes) tirés du contexte de l'onglet. */
export function scopeParams(ctx) {
  const params = { start: ctx.period.from, end: ctx.period.to };
  if (ctx.accountIds.length) params.accounts = ctx.accountIds.join(",");
  if (ctx.compare) {
    params.cstart = ctx.compare.from;
    params.cend = ctx.compare.to;
  }
  return params;
}

export function section(title, ...children) {
  return el("section", { className: "card" }, el("h2", { text: title }), ...children);
}

export function emptyNote(text) {
  return el("p", { className: "placeholder", text });
}

/**
 * Liste déroulante de reclassement : « À classer » puis chaque racine suivie de ses sous-catégories
 * (`categories` = meta.categories : [{id, name, kind, children:[{id, name}]}]).
 */
export function categorySelect(categories, selectedId, onChange, label = "Catégorie") {
  const select = el("select", { attrs: { "aria-label": label } });
  select.append(el("option", { value: "", text: "À classer" }));
  for (const root of categories) {
    const group = el("optgroup", { label: root.name });
    group.append(el("option", { value: String(root.id), text: root.name }));
    for (const child of root.children) {
      group.append(el("option", { value: String(child.id), text: `${root.name} › ${child.name}` }));
    }
    select.append(group);
  }
  select.value = selectedId === null || selectedId === undefined ? "" : String(selectedId);
  select.addEventListener("change", () => onChange(select.value === "" ? null : Number(select.value), select));
  return select;
}

/** Tableau de transactions ; `reclass(tx, categoryId)` est appelé quand la catégorie change. */
export function transactionsTable(rows, { categories, accounts, reclass }) {
  const accountLabel = new Map((accounts || []).map((a) => [a.id, `${a.label} ••${a.number_tail}`]));
  const head = el("thead", {}, el("tr", {},
    ["Date", "Libellé", "Compte", "Montant", "Catégorie"].map((h) =>
      el("th", { scope: "col", className: h === "Montant" ? "num" : "", text: h }))));
  const body = el("tbody");
  for (const tx of rows) {
    const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });
    const select = categorySelect(categories, tx.category_id, async (categoryId, node) => {
      node.disabled = true;
      error.textContent = "";
      try {
        await reclass(tx, categoryId);
      } catch (e) {
        node.value = tx.category_id === null ? "" : String(tx.category_id);
        error.textContent = `Reclassement impossible : ${e.message}`;
      } finally {
        node.disabled = false;
      }
    }, `Catégorie de « ${tx.label} »`);
    const labelCell = el("td", { className: "label" }, el("span", { text: tx.label }));
    if (tx.receipt_id) {
      labelCell.append(el("a", { className: "receipt-icon", href: `#tab=courses&receipt=${tx.receipt_id}`,
                                 text: "🧾", title: "Voir le ticket de caisse", attrs: { "aria-label": "Voir le ticket de caisse" } }));
    }
    if (tx.manual) labelCell.append(el("span", { className: "tag", text: "manuel" }));
    if (tx.mirror) labelCell.append(el("span", { className: "tag", text: "miroir" }));
    if (tx.classified_by === "model") {
      labelCell.append(el("span", { className: "tag auto", text: `auto ${score(tx.confidence)}`,
                                    title: tx.suggestion_reason || "" }));
    }
    body.append(el("tr", {},
      el("td", { className: "date", text: formatDate(tx.op_date) }),
      labelCell,
      el("td", { className: "account", text: accountLabel.get(tx.account_id) || `#${tx.account_id}` }),
      el("td", { className: "num" }, amount(tx.amount)),
      el("td", { className: "category" }, select, error)));
  }
  return el("div", { className: "table-wrap" }, el("table", { className: "tx-table" }, head, body));
}
