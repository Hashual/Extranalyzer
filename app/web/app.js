// Coquille de l'application : connexion, barre commune (période, comparaison, comptes, dépôt,
// pastille « À classer »), onglets chargés dynamiquement depuis /api/tabs.
// L'état (onglet, période, comparaison, comptes, paramètres d'onglet) vit dans le hash de l'URL.
import { api, ApiError, setUnauthorizedHandler } from "./api.js";
import { hideAll, isKindVisible, KINDS, onPrivacyChange, setKindVisible, toggleHideAll } from "./privacy.js";

const $ = (id) => document.getElementById(id);
const RESERVED = new Set(["tab", "from", "to", "cmp", "acc"]);
const COMPARE_MODES = new Set(["none", "previous", "yoy"]);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

const state = { tabs: [], modules: new Map(), meta: null, renderToken: 0 };

// --- Dates (chaînes ISO AAAA-MM-JJ, calculs en UTC pour éviter les décalages horaires) ----------

function iso(date) {
  return date.toISOString().slice(0, 10);
}

function parts(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number);
  return { y, m: m - 1, d };
}

function utc(y, m, d) {
  return iso(new Date(Date.UTC(y, m, d)));
}

function addDays(isoDate, n) {
  const { y, m, d } = parts(isoDate);
  return utc(y, m, d + n);
}

function daysBetween(a, b) {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
}

function monthRange(anchor) {
  const { y, m } = parts(anchor);
  return { from: utc(y, m, 1), to: utc(y, m + 1, 0) };
}

function quarterRange(anchor) {
  const { y, m } = parts(anchor);
  const start = Math.floor(m / 3) * 3;
  return { from: utc(y, start, 1), to: utc(y, start + 3, 0) };
}

function yearRange(anchor) {
  const { y } = parts(anchor);
  return { from: utc(y, 0, 1), to: utc(y, 11, 31) };
}

function shiftYear(isoDate, n) {
  const { y, m, d } = parts(isoDate);
  const lastDay = new Date(Date.UTC(y + n, m + 1, 0)).getUTCDate();
  return utc(y + n, m, Math.min(d, lastDay));
}

/** Nombre de mois entiers couverts (0 si la période ne commence pas un 1er ou ne finit pas un fin de mois). */
function wholeMonths(period) {
  if (!period.from.endsWith("-01") || !addDays(period.to, 1).endsWith("-01")) return 0;
  const f = parts(period.from);
  const t = parts(period.to);
  return (t.y - f.y) * 12 + (t.m - f.m) + 1;
}

function comparePeriod(period, mode) {
  if (mode === "yoy") {
    return { mode, from: shiftYear(period.from, -1), to: shiftYear(period.to, -1) };
  }
  if (mode === "previous") {
    const months = wholeMonths(period);
    if (months > 0) {           // mois, trimestre, année… : la même durée calendaire juste avant
      const f = parts(period.from);
      return { mode, from: utc(f.y, f.m - months, 1), to: addDays(period.from, -1) };
    }
    const to = addDays(period.from, -1);
    return { mode, from: addDays(to, -daysBetween(period.from, period.to)), to };
  }
  return null;
}

function formatDate(isoDate) {
  const { y, m, d } = parts(isoDate);
  return `${String(d).padStart(2, "0")}/${String(m + 1).padStart(2, "0")}/${y}`;
}

// --- État dans le hash -------------------------------------------------------------------------

function readRoute() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const route = {
    tab: params.get("tab"),
    from: params.get("from"),
    to: params.get("to"),
    cmp: params.get("cmp") || "none",
    acc: (params.get("acc") || "").split(",").filter((v) => /^\d+$/.test(v)).map(Number),
    params: {},
  };
  for (const [key, value] of params) if (!RESERVED.has(key)) route.params[key] = value;
  return route;
}

function routeHash(route) {
  const params = new URLSearchParams();
  params.set("tab", route.tab);
  params.set("from", route.from);
  params.set("to", route.to);
  if (route.cmp !== "none") params.set("cmp", route.cmp);
  if (route.acc.length) params.set("acc", route.acc.join(","));
  for (const [key, value] of Object.entries(route.params || {})) {
    if (!RESERVED.has(key) && value !== null && value !== undefined) params.set(key, value);
  }
  return "#" + params.toString();
}

function go(route, { replace = false } = {}) {
  const hash = routeHash(route);
  if (hash === window.location.hash) return renderCurrent();
  if (replace) {
    history.replaceState(null, "", hash);
    return renderCurrent();
  }
  window.location.hash = hash;     // déclenche hashchange → renderCurrent
}

function defaultPeriod() {
  const max = state.meta?.data_range?.max;
  return monthRange(max || iso(new Date()));
}

/** Complète ou corrige la route lue dans l'URL ; renvoie null si elle était déjà valide. */
function normalized(route) {
  const fixed = { ...route, params: { ...route.params } };
  if (!state.tabs.some((t) => t.id === fixed.tab)) {
    fixed.tab = state.tabs[0]?.id;
    fixed.params = {};
  }
  if (!ISO_DATE.test(fixed.from || "") || !ISO_DATE.test(fixed.to || "") || fixed.from > fixed.to) {
    Object.assign(fixed, defaultPeriod());
  }
  if (!COMPARE_MODES.has(fixed.cmp)) fixed.cmp = "none";
  const known = new Set((state.meta?.accounts || []).map((a) => a.id));
  fixed.acc = fixed.acc.filter((id) => known.has(id));
  return routeHash(fixed) === routeHash(route) ? null : fixed;
}

// --- Toasts ------------------------------------------------------------------------------------

function toast(lines, kind = "info", duration = 8000) {
  const box = document.createElement("div");
  box.className = `toast ${kind}`;
  box.setAttribute("role", kind === "error" ? "alert" : "status");
  for (const line of [].concat(lines)) {
    const p = document.createElement("p");
    p.textContent = line;
    box.append(p);
  }
  const close = () => box.remove();
  box.addEventListener("click", close);
  $("toasts").append(box);
  if (duration) setTimeout(close, duration);
  return close;
}

const SECTION_STATUS = {
  ok: (s) => `${s.tx_count} opération(s) importée(s)`,
  error: (s) => `en erreur (écart ${s.gap} €)${s.message ? " : " + s.message : ""}`,
  conflict: () => "diffère d'un relevé déjà importé : à trancher dans l'onglet Relevés",
  superseded: () => "déjà importé, rien de nouveau",
};

function reportLines(report) {
  if (report.error && !report.sections.length) return { kind: "error", lines: [`Import échoué : ${report.error}`] };
  if (report.duplicate_file) return { kind: "info", lines: ["Ce relevé a déjà été importé."] };
  const lines = report.sections.map((s) => {
    const describe = SECTION_STATUS[s.status] || (() => s.status);
    return `Compte ••${s.number_tail} au ${formatDate(s.statement_date)} : ${describe(s)}`;
  });
  if (report.error) lines.push(report.error);
  const failed = report.error || report.sections.some((s) => s.status === "error" || s.status === "conflict");
  return { kind: failed ? "warning" : "success", lines: lines.length ? lines : ["Aucune section trouvée."] };
}

/** Résumé d'un dépôt de plusieurs fichiers : compteurs, puis le détail des seuls fichiers à revoir. */
function summaryLines(results) {
  const count = { ok: 0, duplicates: 0, failed: 0, errors: 0, conflicts: 0 };
  const details = [];
  for (const { name, report, error } of results) {
    if (error) {
      count.failed += 1;
      details.push(`${name} : import échoué (${error})`);
      continue;
    }
    if (report.duplicate_file) count.duplicates += 1;
    else if (report.error && !report.sections.length) count.failed += 1;
    else count.ok += 1;
    count.errors += report.sections.filter((s) => s.status === "error").length;
    count.conflicts += report.sections.filter((s) => s.status === "conflict").length;
    const { kind, lines } = reportLines(report);
    if (kind === "warning" || kind === "error") details.push(...lines.map((line) => `${name} — ${line}`));
  }
  const lines = [
    `${results.length} fichiers : ${count.ok} importé(s), ${count.duplicates} déjà importé(s), ${count.failed} en échec`,
    `Sections en erreur : ${count.errors} · conflits à trancher (onglet Relevés) : ${count.conflicts}`,
    ...details,
  ];
  const problems = count.failed + count.errors + count.conflicts;
  return { kind: problems ? "warning" : "success", lines };
}

/** Dépose les fichiers un par un (une écriture lourde à la fois côté serveur), puis rafraîchit une fois. */
async function uploadFiles(files) {
  const results = [];
  for (const [index, file] of files.entries()) {
    const progress = files.length > 1 ? ` (${index + 1}/${files.length})` : "";
    const dismiss = toast(`Import de ${file.name} en cours…${progress}`, "info", 0);
    try {
      const form = new FormData();
      form.append("file", file);
      results.push({ name: file.name, report: await api.post("/upload", form) });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return;     // session expirée : écran de connexion
      results.push({ name: file.name, error: error.message });
    } finally {
      dismiss();
    }
  }
  const single = results.length === 1 && results[0].report;
  const { kind, lines } = single ? reportLines(results[0].report)
    : results.length === 1 ? { kind: "error", lines: [`Import échoué : ${results[0].error}`] }
    : summaryLines(results);
  toast(lines, kind, kind === "success" ? 8000 : 0);
  await refresh();
}

// --- Barre commune -----------------------------------------------------------------------------

function renderBar(route) {
  $("period-from").value = route.from;
  $("period-to").value = route.to;
  $("compare").value = route.cmp;
  $("bar-toggle").textContent = periodLabel(route.from, route.to);
  $("bar-toggle").title = `Du ${formatDate(route.from)} au ${formatDate(route.to)}`;

  const list = $("accounts-list");
  list.replaceChildren();
  for (const account of state.meta?.accounts || []) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = account.id;
    // Sans sélection explicite : vue par défaut de l'API = comptes courants.
    box.checked = route.acc.length ? route.acc.includes(account.id) : account.kind === "checking";
    label.append(box, ` ${account.label} ••${account.number_tail}`);
    list.append(label);
  }
  $("accounts").querySelector("summary").textContent = accountsLabel(route.acc);

  const count = state.meta?.unclassified_count ?? 0;
  const badge = $("unclassified");
  badge.textContent = `À classer (${count})`;
  badge.disabled = !state.tabs.some((t) => t.id === "a_classer");
  badge.classList.toggle("alert", count > 0);

  renderBankAlert();

  for (const button of $("tabs").querySelectorAll("button")) {
    const current = button.dataset.tab === route.tab;
    button.setAttribute("aria-current", current ? "page" : "false");
    // Barre d'onglets défilante (téléphone) : l'onglet actif reste visible, même ouvert par un lien.
    if (current) button.scrollIntoView({ block: "nearest", inline: "center" });
  }
  // Changer d'onglet replie la barre (téléphone) ; changer de période la laisse ouverte.
  if (state.lastTab && state.lastTab !== route.tab) setBarOpen(false);
  state.lastTab = route.tab;
}

// Pastille commune à toutes les banques connectées : la plus urgente, nommée s'il y en a plusieurs.
const bankName = (b) => (b.links > 1 && b.aspsp_name ? ` ${b.aspsp_name}` : "");
const BANK_ALERTS = {
  expiring: (b) => `Banque : renouveler${bankName(b)} (${b.days_left} j)`,
  expired: (b) => `Banque${bankName(b)} : connexion expirée`,
  error: (b) => `Banque${bankName(b)} : erreur de synchro`,
};

/** Pastille de la connexion bancaire : consentement à renouveler, expiré, erreur ou compte à rattacher. */
function renderBankAlert() {
  const bank = state.meta?.bank;
  const pill = $("bank-alert");
  const text = bank && BANK_ALERTS[bank.status] ? BANK_ALERTS[bank.status](bank)
    : bank?.to_attach ? `Banque : ${bank.to_attach} compte(s) à rattacher` : "";
  pill.textContent = text;
  pill.hidden = !text || !state.tabs.some((t) => t.id === "banque");
}

function accountsLabel(selected) {
  const accounts = state.meta?.accounts || [];
  if (!selected.length) return "Comptes courants";
  if (accounts.length > 1 && selected.length === accounts.length) return "Tous les comptes";
  if (selected.length === 1) {
    const account = accounts.find((a) => a.id === selected[0]);
    if (account) return `${account.label} ••${account.number_tail}`;
  }
  return `${selected.length} comptes`;
}

function updateRoute(changes) {
  go({ ...readRoute(), ...changes });
}

const MONTHS_SHORT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.",
  "déc."];

/** Libellé court de la période pour le bouton de la barre repliée (téléphone) : « oct. 2026 », « T4 2026 »,
 *  « 2026 », sinon les deux dates (« 01/01 – 31/10/26 », l'année n'étant répétée que si elle change). */
function periodLabel(from, to) {
  if (!ISO_DATE.test(from || "") || !ISO_DATE.test(to || "")) return "Période";
  const f = parts(from);
  const t = parts(to);
  const months = wholeMonths({ from, to });
  if (months === 1) return `${MONTHS_SHORT[f.m]} ${f.y}`;
  if (months === 3 && f.m % 3 === 0) return `T${f.m / 3 + 1} ${f.y}`;
  if (months === 12 && f.m === 0) return String(f.y);
  const dm = (p) => `${String(p.d).padStart(2, "0")}/${String(p.m + 1).padStart(2, "0")}`;
  const yy = (p) => String(p.y).slice(2);
  return f.y === t.y ? `${dm(f)} – ${dm(t)}/${yy(t)}` : `${dm(f)}/${yy(f)} – ${dm(t)}/${yy(t)}`;
}

function setBarOpen(open) {
  document.querySelector(".bar").classList.toggle("open", open);
  $("bar-toggle").setAttribute("aria-expanded", String(open));
}

function setupBar() {
  // Sur téléphone, la barre est repliée : ce bouton affiche la période et déplie les autres commandes.
  $("bar-toggle").addEventListener("click", () => {
    setBarOpen(!document.querySelector(".bar").classList.contains("open"));
  });
  // Fondu du bord droit des onglets (téléphone) : retiré quand on a défilé jusqu'au dernier.
  const nav = $("tabs");
  const markEnd = () => nav.classList.toggle("at-end", nav.scrollLeft + nav.clientWidth >= nav.scrollWidth - 2);
  nav.addEventListener("scroll", markEnd, { passive: true });
  window.addEventListener("resize", markEnd);
  new MutationObserver(markEnd).observe(nav, { childList: true });

  const onDates = () => {
    const from = $("period-from").value;
    const to = $("period-to").value;
    if (ISO_DATE.test(from) && ISO_DATE.test(to) && from <= to) updateRoute({ from, to });
  };
  $("period-from").addEventListener("change", onDates);
  $("period-to").addEventListener("change", onDates);

  for (const button of document.querySelectorAll("[data-shortcut]")) {
    button.addEventListener("click", () => {
      const anchor = readRoute().to || iso(new Date());
      const kind = button.dataset.shortcut;
      if (kind === "month") updateRoute(monthRange(anchor));
      else if (kind === "quarter") updateRoute(quarterRange(anchor));
      else if (kind === "year") updateRoute(yearRange(anchor));
      else {
        const range = state.meta?.data_range;
        // Fin = max(dernière opération, aujourd'hui) : le tableau de bord d'épargne porte sur les avoirs actuels.
        if (range?.min && range?.max) {
          const today = iso(new Date());
          updateRoute({ from: range.min, to: range.max > today ? range.max : today });
        }
      }
    });
  }

  $("compare").addEventListener("change", (event) => updateRoute({ cmp: event.target.value }));

  $("accounts-list").addEventListener("change", () => {
    // Sélection explicite, même si tout est coché (sinon retour à la vue « comptes courants »).
    const checked = [...$("accounts-list").querySelectorAll("input")].filter((b) => b.checked)
      .map((b) => Number(b.value));
    updateRoute({ acc: checked });
  });

  // La pastille compte toutes les opérations à classer : elle ouvre l'onglet sur toute la plage de
  // données et tous les comptes (sélection explicite), pour montrer exactement cette population.
  $("unclassified").addEventListener("click", () => {
    if (!state.tabs.some((t) => t.id === "a_classer")) return;
    const range = state.meta?.data_range;
    const everything = range?.min && range?.max ? { from: range.min, to: range.max } : {};
    const acc = (state.meta?.accounts || []).map((a) => a.id);
    go({ ...readRoute(), ...everything, acc, tab: "a_classer", params: {} });
  });

  $("bank-alert").addEventListener("click", () => navigate("banque"));

  $("upload-input").addEventListener("change", async (event) => {
    const input = event.target;
    const files = [...input.files];
    input.value = "";
    if (!files.length) return;
    try {
      await uploadFiles(files);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) toast(`Import échoué : ${error.message}`, "error", 0);
    }
  });

  $("logout").addEventListener("click", async () => {
    try {
      await api.post("/logout");
    } finally {
      showLogin();
    }
  });
}

// --- Masquage des montants --------------------------------------------------------------------

function renderPrivacy() {
  const all = hideAll();
  const toggle = $("privacy-toggle");
  toggle.textContent = all ? "Afficher les montants" : "Masquer les montants";
  toggle.setAttribute("aria-pressed", String(all));
  for (const box of $("privacy-list").querySelectorAll("input")) {
    box.checked = isKindVisible(box.value);
    box.disabled = all;           // tout est masqué : les cases reprennent effet au réaffichage
  }
  $("privacy-note").hidden = !all;
}

function setupPrivacy() {
  const list = $("privacy-list");
  list.append(Object.assign(document.createElement("p"), {
    className: "privacy-title", textContent: "Montants affichés" }));
  for (const [kind, label] of KINDS) {
    const row = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = kind;
    box.addEventListener("change", () => setKindVisible(kind, box.checked));
    row.append(box, ` ${label}`);
    list.append(row);
  }
  const note = Object.assign(document.createElement("p"), {
    id: "privacy-note", className: "muted privacy-note", textContent: "Tous les montants sont masqués." });
  list.append(note);
  $("privacy-toggle").addEventListener("click", toggleHideAll);
  document.addEventListener("click", (event) => {       // le menu se ferme en cliquant ailleurs
    if (!$("privacy-menu").contains(event.target)) $("privacy-menu").open = false;
  });
  onPrivacyChange(() => {
    renderPrivacy();
    renderCurrent();
  });
  renderPrivacy();
}

// --- Onglets -----------------------------------------------------------------------------------

function navigate(tabId, params = {}) {
  if (!state.tabs.some((t) => t.id === tabId)) return;
  go({ ...readRoute(), tab: tabId, params });
}

async function refresh() {
  state.meta = await api.get("/meta");
  return renderCurrent();
}

function buildTabs() {
  const nav = $("tabs");
  nav.replaceChildren();
  for (const tab of state.tabs) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.tab = tab.id;
    button.textContent = tab.title;
    button.addEventListener("click", () => navigate(tab.id));
    nav.append(button);
  }
}

async function loadModule(tabId) {
  if (!state.modules.has(tabId)) state.modules.set(tabId, import(`./tabs/${tabId}.js`));
  try {
    return await state.modules.get(tabId);
  } catch (error) {
    state.modules.delete(tabId);    // nouvel essai au prochain affichage
    throw error;
  }
}

function showMessage(container, text, className = "placeholder") {
  const p = document.createElement("p");
  p.className = className;
  p.textContent = text;
  container.replaceChildren(p);
}

async function renderCurrent() {
  if (!state.tabs.length) return;
  const route = readRoute();
  const fixed = normalized(route);
  if (fixed) return go(fixed, { replace: true });

  renderBar(route);
  const token = ++state.renderToken;
  const container = $("tab-container");
  const period = { from: route.from, to: route.to };
  const ctx = {
    period,
    compare: comparePeriod(period, route.cmp),
    accountIds: route.acc,          // vide = vue par défaut (comptes courants)
    enabledTabs: state.tabs.map((t) => t.id),
    params: route.params,
    api,
    navigate,
    refresh,
  };
  try {
    const module = await loadModule(route.tab);
    if (token !== state.renderToken) return;      // un affichage plus récent a pris la main
    await module.render(container, ctx);
  } catch (error) {
    if (token !== state.renderToken || (error instanceof ApiError && error.status === 401)) return;
    showMessage(container, `Impossible d'afficher l'onglet : ${error.message}`, "error");
  }
}

// --- Connexion et démarrage ---------------------------------------------------------------------

function showLogin() {
  state.tabs = [];
  state.meta = null;
  $("app").hidden = true;
  $("tab-container").replaceChildren();
  $("login").hidden = false;
  $("password").value = "";
  $("password").focus();
}

async function showApp() {
  [state.tabs, state.meta] = await Promise.all([api.get("/tabs"), api.get("/meta")]);
  $("login").hidden = true;
  $("app").hidden = false;
  buildTabs();
  await renderCurrent();
}

function setupLogin() {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("login-error");
    error.textContent = "";
    try {
      await api.post("/login", { password: $("password").value });
      await showApp();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) error.textContent = "Mot de passe incorrect.";
      else if (e instanceof ApiError && e.status === 429) error.textContent = "Trop de tentatives. Réessayez dans une minute.";
      else error.textContent = `Connexion impossible : ${e.message}`;
    }
  });
}

setUnauthorizedHandler(showLogin);
setupLogin();
setupBar();
setupPrivacy();
window.addEventListener("hashchange", () => renderCurrent());
showApp().catch((error) => {
  if (!(error instanceof ApiError && error.status === 401)) toast(`Chargement impossible : ${error.message}`, "error", 0);
});
