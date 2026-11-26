// Petits utilitaires d'affichage partagés par les onglets.
// Tout texte venant du serveur passe par textContent (les libellés viennent des relevés bancaires).
import { isMasked, MASK, signKind } from "./privacy.js";

const EUR = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const PCT = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 1, signDisplay: "exceptZero" });
const SCORE = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 0 });
const MONTH = new Intl.DateTimeFormat("fr-FR", { month: "short", year: "numeric", timeZone: "UTC" });

/** Lien vers la fiche d'un ticket en conservant période, comparaison et comptes de la route courante. */
function receiptHref(receiptId) {
  const current = new URLSearchParams(window.location.hash.slice(1));
  const next = new URLSearchParams();
  next.set("tab", "courses");
  for (const key of ["from", "to", "cmp", "acc"]) if (current.get(key)) next.set(key, current.get(key));
  next.set("receipt", receiptId);
  return "#" + next.toString();
}

/**
 * Montant (chaîne "123.45" de l'API) → « 123,45 € », ou « ****** » si son type est masqué.
 * `kind` : expense, income, savings ou balance (voir privacy.js) ; par défaut, selon le signe.
 */
export function money(value, kind = signKind(value)) {
  return isMasked(kind) ? MASK : EUR.format(Number(value));
}

/** Variation signée (« +12,00 € », « -3,00 € »), masquée comme un montant. */
export function signedMoney(value, kind) {
  if (isMasked(kind)) return MASK;
  return `${Number(value) > 0 ? "+" : ""}${EUR.format(Number(value))}`;
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

/** Texte lu par les lecteurs d'écran mais invisible à l'écran. */
export function visuallyHidden(text) {
  return el("span", { className: "visually-hidden", text });
}

/**
 * Montant coloré : négatif en rouge, positif en vert (classes CSS ; le signe « - » porte aussi l'information) ;
 * masqué, sans couleur (le signe se lirait) et annoncé « Montant masqué » plutôt que six astérisques.
 */
export function amount(value, kind = signKind(value), tag = "span") {
  const n = Number(value);
  if (isMasked(kind)) {
    return el(tag, { className: "amount masked" }, el("span", { text: MASK, attrs: { "aria-hidden": "true" } }),
      visuallyHidden("Montant masqué"));
  }
  return el(tag, { className: `amount ${n < 0 ? "neg" : n > 0 ? "pos" : ""}`.trim(), text: money(value, kind) });
}

/**
 * Barre horizontale proportionnelle : la largeur et la couleur passent au CSS par des propriétés
 * personnalisées (--fill, --fill-color), le style reste dans la feuille.
 */
export function setFill(node, ratio, color) {
  node.style.setProperty("--fill", `${Math.min(Math.max(ratio, 0), 1) * 100}%`);
  if (color) node.style.setProperty("--fill-color", color);
  return node;
}

// --- Boîtes de dialogue modales (remplacent window.confirm / window.prompt) -------------------------

let dialogCount = 0;

/**
 * Ouvre un <dialog> modal natif (focus piégé, Échap, clic hors de la boîte) et renvoie une promesse :
 * la valeur saisie (ou "" sans champ) si l'utilisateur valide, null s'il annule.
 */
function openDialog({ message, input = null, confirmLabel, cancelLabel = "Annuler", danger = false }) {
  const { promise, resolve } = Promise.withResolvers();
  const textId = `dialog-${++dialogCount}-text`;
  const text = input
    ? el("label", { id: textId, htmlFor: input.id, className: "dialog-message", text: message })
    : el("p", { id: textId, className: "dialog-message", text: message });
  // Bouton de validation d'abord dans le DOM : Entrée dans le champ soumet avec lui.
  const actions = el("div", { className: "dialog-actions" },
    el("button", { type: "submit", value: "ok", className: danger ? "danger" : "primary", text: confirmLabel }),
    // Action destructrice : le focus initial va sur « Annuler », jamais sur l'action irréversible.
    el("button", { type: "submit", value: "cancel", text: cancelLabel, autofocus: danger, attrs: { formnovalidate: "" } }));
  const form = el("form", { method: "dialog" }, text, input, actions);
  const dialog = el("dialog", { className: "dialog", attrs: { "aria-labelledby": textId, closedby: "any" } }, form);
  dialog.addEventListener("close", () => {
    const result = dialog.returnValue === "ok" ? (input ? input.value : "") : null;
    dialog.remove();
    resolve(result);
  });
  document.body.append(dialog);
  dialog.showModal();
  return promise;
}

/** Demande une confirmation ; résout à true si l'utilisateur confirme. */
export async function confirmDialog(message, { confirmLabel = "Confirmer", danger = false } = {}) {
  return (await openDialog({ message, confirmLabel, danger })) !== null;
}

/** Demande une valeur ; résout à la chaîne saisie, ou null si l'utilisateur annule. */
export function promptDialog(message, { value = "", inputMode = null, confirmLabel = "Valider" } = {}) {
  const input = el("input", { type: "text", id: `dialog-${dialogCount + 1}-input`, value, autofocus: true,
                              attrs: inputMode ? { inputmode: inputMode, autocomplete: "off" } : { autocomplete: "off" } });
  return openDialog({ message, input, confirmLabel });
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
  // Type de montant selon la catégorie (une sous-catégorie a le type de sa racine), sinon selon le signe.
  const kindOf = new Map(categories.flatMap((root) =>
    [[root.id, root.kind], ...root.children.map((child) => [child.id, root.kind])]));
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
      labelCell.append(el("a", { className: "receipt-icon", href: receiptHref(tx.receipt_id) },
                          el("span", { text: "🧾", attrs: { "aria-hidden": "true" } }),
                          visuallyHidden("Voir le ticket de caisse")));
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
      el("td", { className: "num" }, amount(tx.amount, kindOf.get(tx.category_id) || signKind(tx.amount))),
      el("td", { className: "category" }, select, error)));
  }
  return el("div", { className: "table-wrap" },
    el("table", { className: "tx-table" }, el("caption", { className: "visually-hidden", text: "Opérations" }), head, body));
}
