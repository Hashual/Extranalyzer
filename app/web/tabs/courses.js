// Onglet « Courses » : consommation par famille de rayons (tickets de caisse), tickets et rapprochement
// avec les débits, rayons à ranger, relève des tickets et dépôt manuel.
// Paramètres d'onglet : `family=<id>` (détail d'une famille, 0 = à ranger), `receipt=<id>` (fiche d'un ticket),
// `receipts=<état>` (liste filtrée : to_link, broken, error, pending_parser).
import { amount, el, emptyNote, formatDate, money, scopeParams, section, setFill, signedMoney } from "../ui.js";

export const id = "courses";
export const title = "Courses";

const BASE = "/tabs/courses";
const PAYMENTS = [["card", "Carte bancaire"], ["meal_voucher", "Titres-restaurant"], ["voucher", "Bons et cagnotte"],
                  ["cash", "Espèces"], ["other", "Autre"]];
const PAYMENT_LABEL = Object.fromEntries(PAYMENTS);
const LINKS = { auto: "relié auto", manual: "relié à la main", to_link: "à relier", broken: "débit disparu",
                no_card: "sans débit bancaire" };
const STATUSES = { error: "en erreur", pending_parser: "en attente de lecteur" };
const WARN = new Set(["to_link", "broken", "error", "pending_parser"]);

function badge(receipt) {
  const key = receipt.link || receipt.status;
  const text = LINKS[receipt.link] || STATUSES[receipt.status] || receipt.status;
  return el("span", { className: `tag${WARN.has(key) ? " warn" : ""}`, text, title: receipt.message || "" });
}

function back(ctx, text) {
  return el("button", { type: "button", className: "link", text, on: { click: () => ctx.navigate("courses") } });
}

function shortDate(iso) {
  return iso ? formatDate(iso.slice(0, 10)) : "date inconnue";
}

function table(heads, rows) {
  const head = el("thead", {}, el("tr", {}, heads.map((h) =>
    el("th", { scope: "col", className: h.endsWith("#") ? "num" : "", text: h.replace(/#$/, "") }))));
  return el("div", { className: "table-wrap" }, el("table", { className: "data-table" }, head, el("tbody", {}, rows)));
}

function num(node) {
  return el("td", { className: "num" }, node);
}

function localTime(iso) {
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString("fr-FR") : String(iso || "");
}

function quantity(value) {
  return String(value).replace(".", ",");
}

function variation(value, compare) {
  if (compare === null || compare === undefined) return null;
  const diff = Number(value) - Number(compare);
  return el("span", { className: "muted", text: ` (${signedMoney(diff, "expense")})` });
}

// --- Synthèse ------------------------------------------------------------------------------------

function header(ctx, data) {
  const c = data.counts;
  const open = (params) => () => ctx.navigate("courses", params);
  const alerts = [
    [c.to_link, "à relier", open({ receipts: "to_link" })],
    [c.unsorted_aisles, "rayon(s) à ranger", () => {
      const panel = document.getElementById("courses-aisles");
      if (!panel) return;
      panel.open = true;
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }],
    [c.errors, "en erreur", open({ receipts: "error" })],
    [c.pending_parser, "en attente de lecteur", open({ receipts: "pending_parser" })],
    [c.broken, "débit(s) disparu(s)", open({ receipts: "broken" })],
  ].filter(([n]) => n > 0);
  const payments = PAYMENTS.filter(([key]) => Number(data.by_payment[key]));
  return [
    el("div", { className: "tiles" },
      el("div", { className: "tile" },
        el("p", { className: "tile-label", text: "Courses" }),
        el("p", { className: "tile-value" }, money(data.total, "expense"), variation(data.total, data.total_compare))),
      payments.map(([key, label]) => el("div", { className: "tile" },
        el("p", { className: "tile-label", text: label }),
        el("p", { className: "tile-value", text: money(data.by_payment[key], "expense") })))),
    alerts.length ? el("div", { className: "alerts" },
      alerts.map(([n, label, go]) => el("button", { type: "button", className: "pill alert", text: `${n} ${label}`,
                                                    on: { click: go } })),
      c.to_link > 0 ? el("p", { className: "tile-note",
        text: `Total provisoire : ${c.to_link} ticket(s) à relier peuvent être comptés deux fois.` }) : null) : null,
  ];
}

function familyBars(ctx, data) {
  const undetailed = Number(data.undetailed);
  const max = Math.max(...data.families.map((f) => Number(f.amount)), undetailed, 0);
  const bar = (name, value, compare, onClick) => {
    const fill = setFill(el("span", { className: "bar-fill" }), max > 0 ? Number(value) / max : 0);
    return el("li", {}, el("button", { type: "button", className: "bar-row-button", disabled: !onClick,
                                       on: { click: () => onClick && onClick() } },
      el("span", { className: "bar-name", text: name }),
      el("span", { className: "bar-track" }, fill),
      el("span", { className: "bar-amount" }, money(value, "expense"), variation(value, compare))));
  };
  const items = data.families.map((f) =>
    bar(f.name, f.amount, f.compare, () => ctx.navigate("courses", { family: f.id })));
  if (undetailed || Number(data.undetailed_compare)) {
    items.push(bar("Non détaillé", data.undetailed, data.undetailed_compare, null));
  }
  return items.length ? el("ul", { className: "bars" }, items) : emptyNote("Aucun ticket sur la période.");
}

function outOfCourses(ctx, items) {
  if (!items.length) return null;
  return section("Tickets reliés à un débit hors Courses",
    el("ul", { className: "plain-list" }, items.map((i) => el("li", {},
      el("button", { type: "button", className: "link", text: i.tx_label,
                     on: { click: () => ctx.navigate("courses", { receipt: i.receipt_id }) } }),
      el("span", { className: "muted", text: i.category_name || "À classer" })))));
}

// --- Détail d'une famille ------------------------------------------------------------------------

async function familyView(container, ctx, familyId) {
  const data = await ctx.api.get(`${BASE}/family/${familyId}`, scopeParams(ctx));
  const blocks = data.aisles.map((a) => section(`${a.name} · ${money(a.amount, "expense")}`,
    el("p", { className: "muted", text: `${a.store}${a.compare !== null ? ` · comparaison ${money(a.compare, "expense")}` : ""}` }),
    a.products.length ? table(["Produit", "Qté#", "Montant#", "Prix moyen#", "Achats#"],
      a.products.map((p) => el("tr", {},
        el("td", { className: "label", text: p.label }), num(quantity(p.quantity)), num(money(p.amount, "expense")),
        num(p.avg_unit === null ? "—" : money(p.avg_unit, "expense")), num(String(p.purchases))))) : emptyNote("Aucun produit.")));
  container.replaceChildren(back(ctx, "← Courses"),
    ...(blocks.length ? blocks : [emptyNote("Aucun rayon sur la période.")]));
}

// --- Liste et fiche des tickets ------------------------------------------------------------------

function receiptTitle(r) {
  return `${r.store || "Ticket"}${r.shop ? ` · ${r.shop}` : ""}`;
}

function receiptsList(ctx, receipts, filter) {
  const shown = filter ? receipts.filter((r) => r.link === filter || r.status === filter) : receipts;
  const rows = shown.map((r) => el("tr", {},
    el("td", { className: "date", text: shortDate(r.purchased_at) }),
    el("td", {}, el("button", { type: "button", className: "link", text: receiptTitle(r),
                                on: { click: () => ctx.navigate("courses", { receipt: r.id }) } }), badge(r)),
    num(r.total === null ? "—" : money(r.total, "expense"))));
  return section("Tickets", filter ? back(ctx, "Voir tous les tickets") : null,
    shown.length ? table(["Date", "Ticket", "Total#"], rows) : emptyNote("Aucun ticket."));
}

// Suppression en deux temps, sans boîte de dialogue : le premier clic demande confirmation sur place.
function deleteAction(ctx, receiptId) {
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const zone = el("div", { className: "row-actions" });
  const ask = () => {
    error.textContent = "";
    zone.replaceChildren(
      el("p", { className: "muted", text: "Le ticket ne sera pas réimporté par la relève, mais vous pourrez le remettre en déposant à nouveau son PDF." }),
      el("button", { type: "button", className: "danger", text: "Confirmer la suppression", on: { click: confirmDelete } }),
      el("button", { type: "button", text: "Annuler", on: { click: idle } }), error);
  };
  const idle = () => zone.replaceChildren(
    el("button", { type: "button", className: "danger", text: "Supprimer ce ticket", on: { click: ask } }), error);
  const confirmDelete = async (event) => {
    event.currentTarget.disabled = true;
    try {
      await ctx.api.delete(`${BASE}/receipts/${receiptId}`);
      ctx.navigate("courses");      // retour à la liste, dont le résumé est relu
    } catch (e) {
      error.textContent = `Suppression impossible : ${e.message}`;
      event.currentTarget.disabled = false;
    }
  };
  idle();
  return zone;
}

async function receiptView(container, ctx, receiptId) {
  const r = await ctx.api.get(`${BASE}/receipts/${receiptId}`);
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const link = async (txKey) => {
    error.textContent = "";
    try {
      await ctx.api.post(`${BASE}/receipts/${receiptId}/link`, { tx_key: txKey });
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Liaison impossible : ${e.message}`;
    }
  };

  const head = el("div", {}, el("strong", { text: receiptTitle(r) }), badge(r),
    el("p", { className: "muted", text: `${shortDate(r.purchased_at)}${r.total === null ? "" : ` · ${money(r.total, "expense")}`}` }),
    r.message ? el("p", { className: "muted", text: r.message }) : null,
    el("a", { href: `/api/tabs/courses/receipts/${r.id}/pdf`, target: "_blank", rel: "noopener", text: "Ouvrir le PDF" }));

  let debit;
  if (r.tx) {
    debit = el("div", {},
      el("p", {}, `${formatDate(r.tx.op_date)} · ${r.tx.label} · `, amount(r.tx.amount)),
      el("button", { type: "button", text: "Délier", on: { click: () => link(null) } }));
  } else if (r.link === "to_link" || r.link === "broken") {
    debit = r.candidates.length ? el("ul", { className: "plain-list" }, r.candidates.map((c) => el("li", {},
      el("span", { className: "label", text: `${formatDate(c.op_date)} · ${c.label}` }),
      amount(c.amount),
      el("button", { type: "button", text: "Relier", on: { click: () => link(c.tx_key) } }))))
      : emptyNote("Aucun débit candidat (±7 jours).");
  } else {
    debit = emptyNote(r.link === "no_card" ? "Payé sans carte bancaire : pas de débit à relier." : "Pas de débit relié.");
  }

  container.replaceChildren(back(ctx, "← Courses"), section("Ticket", head),
    ...r.lines.map((g) => section(`${g.aisle || "Sans rayon"}${g.family ? ` · ${g.family}` : ""}`,
      table(["Article", "Qté#", "Montant#"], g.lines.map((l) => el("tr", {},
        el("td", { className: "label", text: l.label }), num(quantity(l.quantity)), num(el("span", { className: l.kind === "discount" ? "muted" : "", text: money(l.amount, "expense") }))))))),
    section("Paiements", r.payments.length ? el("ul", { className: "plain-list" }, r.payments.map((p) => el("li", {},
      el("span", { text: PAYMENT_LABEL[p.method] || p.method }), el("span", { text: money(p.amount, "expense") }))))
      : emptyNote("Aucun paiement lu.")),
    section("Débit bancaire", debit, error),
    section("Suppression", deleteAction(ctx, receiptId)));
}

// --- Rayons et familles, relève, dépôt -----------------------------------------------------------

async function aislesPanel(ctx) {
  const data = await ctx.api.get(`${BASE}/aisles`);
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  const run = async (fn) => {
    error.textContent = "";
    try {
      await fn();
      await ctx.refresh();
    } catch (e) {
      error.textContent = e.message;
    }
  };
  const rows = data.aisles.map((a) => {
    const select = el("select", { attrs: { "aria-label": `Famille du rayon ${a.name}` } },
      el("option", { value: "", text: "À ranger" }),
      data.families.map((f) => el("option", { value: String(f.id), text: f.name })));
    select.value = a.family_id === null ? "" : String(a.family_id);
    select.addEventListener("change", () => run(() => ctx.api.post(`${BASE}/aisles/${a.id}`,
      { family_id: select.value === "" ? null : Number(select.value) })));
    return el("tr", {}, el("td", { className: "label", text: a.name }), el("td", { text: a.store }),
      el("td", {}, select));
  });
  const name = el("input", { type: "text", maxLength: 60, placeholder: "Nouvelle famille",
                             attrs: { "aria-label": "Nom de la nouvelle famille" } });
  const create = el("button", { type: "button", text: "Créer", on: { click: () => name.value.trim() &&
    run(() => ctx.api.post(`${BASE}/families`, { name: name.value })) } });
  return el("details", { className: "card", id: "courses-aisles" }, el("summary", { text: "Rayons et familles" }),
    rows.length ? table(["Rayon", "Enseigne", "Famille"], rows) : emptyNote("Aucun rayon lu."),
    el("div", { className: "actions" }, name, create), error);
}

async function syncPanel(ctx) {
  const state = await ctx.api.get(`${BASE}/sync`);
  const message = el("p", { className: "muted", attrs: { role: "status" } });
  const call = (button, path, done) => async () => {
    button.disabled = true;
    message.textContent = "En cours…";
    try {
      await ctx.api.post(path);
      await ctx.refresh();
      message.textContent = done;
    } catch (e) {
      message.textContent = e.message;
    }
    button.disabled = false;
  };
  const now = el("button", { type: "button", text: "Relever maintenant", hidden: !state.enabled });
  now.addEventListener("click", call(now, `${BASE}/sync`, "Relève terminée."));
  const reread = el("button", { type: "button", text: "Relire les tickets" });
  reread.addEventListener("click", call(reread, `${BASE}/reread`, "Tickets relus."));
  const labels = state.labels.map((l) => el("li", {},
    el("span", { className: "label", text: `${l.store} · ${l.label}` }),
    el("span", { className: l.last_error ? "error" : "muted",
                 text: l.last_error || (l.last_success_at
                   ? `relevé le ${localTime(l.last_success_at)}` : "jamais relevé") })));
  return el("details", { className: "card" }, el("summary", { text: "Relève des tickets" }),
    el("p", { className: "muted", text: state.enabled ? "Relève automatique activée."
      : "Relève automatique désactivée : connectez Gmail dans l'onglet Connexions." }),
    labels.length ? el("ul", { className: "plain-list" }, labels) : null,
    el("div", { className: "actions" }, now, reread), message);
}

const OUTCOMES = { stored: "ajouté", slip: "ticket CB enregistré", duplicate: "déjà présent", rejected: "refusé" };

function uploadZone(ctx) {
  const results = el("ul", { className: "plain-list", attrs: { role: "status" } });
  const input = el("input", { type: "file", multiple: true, accept: "application/pdf",
                              attrs: { "aria-label": "Déposer des tickets PDF" } });
  input.addEventListener("change", async () => {
    const files = [...input.files];
    if (!files.length) return;
    results.replaceChildren(el("li", { text: "Envoi en cours…" }));
    try {
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      const out = await ctx.api.post(`${BASE}/upload`, form);
      results.replaceChildren(...out.map((r) => el("li", {}, el("span", { className: "label", text: r.name }),
        el("span", { className: r.outcome === "rejected" ? "error" : "muted",
                     text: `${OUTCOMES[r.outcome] || r.outcome}${r.message ? ` : ${r.message}` : ""}` }))));
    } catch (e) {
      results.replaceChildren(el("li", { className: "error", text: `Envoi impossible : ${e.message}` }));
    }
    input.value = "";
  });
  return section("Déposer des tickets", input, results);
}

// --- Rendu ---------------------------------------------------------------------------------------

export async function render(container, ctx) {
  const { receipt, family, receipts: filter } = ctx.params || {};
  if (/^\d+$/.test(receipt || "")) return receiptView(container, ctx, Number(receipt));
  if (/^\d+$/.test(family || "")) return familyView(container, ctx, Number(family));

  const [data, list, aisles, sync] = await Promise.all([
    ctx.api.get(`${BASE}/summary`, scopeParams(ctx)), ctx.api.get(`${BASE}/receipts`, scopeParams(ctx)),
    aislesPanel(ctx), syncPanel(ctx)]);
  container.replaceChildren(...[...header(ctx, data),
    section("Par famille", familyBars(ctx, data)), outOfCourses(ctx, data.out_of_courses),
    receiptsList(ctx, list, filter), uploadZone(ctx), aisles, sync].filter(Boolean));
}
