// Coquille de l'application : connexion, barre commune (période, comparaison, comptes, dépôt,
// pastille « À classer »), onglets chargés dynamiquement depuis /api/tabs.
// L'état (onglet, période, comparaison, comptes, paramètres d'onglet) vit dans le hash de l'URL.
import { api, ApiError, setUnauthorizedHandler } from "./api.js";

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

// --- Barre commune -----------------------------------------------------------------------------

function renderBar(route) {
  $("period-from").value = route.from;
  $("period-to").value = route.to;
  $("compare").value = route.cmp;

  const list = $("accounts-list");
  list.replaceChildren();
  for (const account of state.meta?.accounts || []) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = account.id;
    box.checked = !route.acc.length || route.acc.includes(account.id);
    label.append(box, ` ${account.label} ••${account.number_tail}`);
    list.append(label);
  }
  $("accounts").querySelector("summary").textContent =
    route.acc.length ? `${route.acc.length} compte(s)` : "Tous les comptes";

  const count = state.meta?.unclassified_count ?? 0;
  const badge = $("unclassified");
  badge.textContent = `À classer (${count})`;
  badge.disabled = !state.tabs.some((t) => t.id === "a_classer");
  badge.classList.toggle("alert", count > 0);

  for (const button of $("tabs").querySelectorAll("button")) {
    button.setAttribute("aria-current", button.dataset.tab === route.tab ? "page" : "false");
  }
}

function updateRoute(changes) {
  go({ ...readRoute(), ...changes });
}

function setupBar() {
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
        if (range?.min && range?.max) updateRoute({ from: range.min, to: range.max });
      }
    });
  }

  $("compare").addEventListener("change", (event) => updateRoute({ cmp: event.target.value }));

  $("accounts-list").addEventListener("change", () => {
    const boxes = [...$("accounts-list").querySelectorAll("input")];
    const checked = boxes.filter((b) => b.checked).map((b) => Number(b.value));
    updateRoute({ acc: checked.length === boxes.length ? [] : checked });
  });

  $("unclassified").addEventListener("click", () => navigate("a_classer"));

  $("upload-input").addEventListener("change", async (event) => {
    const input = event.target;
    const file = input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const dismiss = toast(`Import de ${file.name} en cours…`, "info", 0);
    try {
      const { kind, lines } = reportLines(await api.post("/upload", form));
      toast(lines, kind, kind === "success" ? 8000 : 0);
      await refresh();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) toast(`Import échoué : ${error.message}`, "error", 0);
    } finally {
      dismiss();
      input.value = "";
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
    accountIds: route.acc,          // vide = tous les comptes
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
window.addEventListener("hashchange", () => renderCurrent());
showApp().catch((error) => {
  if (!(error instanceof ApiError && error.status === 401)) toast(`Chargement impossible : ${error.message}`, "error", 0);
});
