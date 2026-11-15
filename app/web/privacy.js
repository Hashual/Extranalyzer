// Masquage des montants (mode « discret », comme un portefeuille crypto) : un bouton cache tout,
// un menu à cases choisit les types de montants affichés. Préférence propre au navigateur
// (localStorage) : rien n'est envoyé au serveur.

export const KINDS = [
  ["expense", "Dépenses"],
  ["income", "Revenus"],
  ["savings", "Épargne"],
  ["balance", "Soldes et écarts"],
];

/** Remplace un montant masqué ; longueur fixe pour ne rien laisser deviner de l'ordre de grandeur. */
export const MASK = "******";

const STORAGE_KEY = "extranalyzer.privacy";
const KNOWN = new Set(KINDS.map(([kind]) => kind));

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    return {
      hideAll: saved.hideAll === true,
      hidden: new Set((Array.isArray(saved.hidden) ? saved.hidden : []).filter((k) => KNOWN.has(k))),
    };
  } catch {
    return { hideAll: false, hidden: new Set() };
  }
}

const state = load();
const listeners = new Set();

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ hideAll: state.hideAll, hidden: [...state.hidden] }));
  } catch {
    // navigation privée ou stockage bloqué : la préférence vaut pour cette page seulement
  }
}

function changed() {
  save();
  for (const listener of listeners) listener();
}

/** Type d'un montant signé (opération) : débit = dépense, crédit = revenu. */
export function signKind(value) {
  return Number(value) < 0 ? "expense" : "income";
}

/** Vrai si les montants de type `kind` doivent être masqués. */
export function isMasked(kind) {
  return state.hideAll || state.hidden.has(kind);
}

export function hideAll() {
  return state.hideAll;
}

export function isKindVisible(kind) {
  return !state.hidden.has(kind);
}

export function toggleHideAll() {
  state.hideAll = !state.hideAll;
  changed();
}

export function setKindVisible(kind, visible) {
  if (!KNOWN.has(kind)) return;
  if (visible) state.hidden.delete(kind);
  else state.hidden.add(kind);
  changed();
}

/** Abonnement aux changements ; renvoie la fonction de désabonnement. */
export function onPrivacyChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
