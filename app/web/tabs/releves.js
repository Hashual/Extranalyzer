// Onglet « Relevés » : relevés importés, statut de réconciliation de chaque section, conflits à trancher
// et ré-analyse de tous les relevés (routes du cœur /sections/{id}/resolve et /reanalyse).
import { el, emptyNote, formatDate, money, section } from "../ui.js";

export const id = "releves";
export const title = "Relevés";

const STATUS = {
  ok: "Réconcilié",
  error: "En erreur",
  conflict: "Conflit",
  superseded: "Écarté",
};

let lastReports = null;      // résultat de la dernière ré-analyse, affiché une fois après le rafraîchissement
let lastNotice = null;       // avertissement d'une action (conflit tranché), affiché une fois

function orphanedNote(n) {
  return n > 0 ? `${n} reclassement(s) manuel(s) sans opération correspondante (à refaire)` : "";
}

function account(s) {
  return `${s.account_label} ••${s.number_tail}`;
}

function sectionNote(s) {
  if (s.status === "error") return s.message || "écart de réconciliation";
  if (s.status === "conflict") return "Diffère d'une version déjà importée de ce relevé.";
  if (s.status === "superseded") return "Version non retenue : aucune opération importée.";
  return "";
}

function conflictActions(s, ctx, error) {
  const resolve = async (accept, button) => {
    const question = accept
      ? `Remplacer les opérations de ${account(s)} au ${formatDate(s.statement_date)} par cette nouvelle version ?`
      : `Garder la version déjà importée de ${account(s)} au ${formatDate(s.statement_date)} et écarter celle-ci ?`;
    if (!window.confirm(question)) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const result = await ctx.api.post(`/sections/${s.id}/resolve`, { accept });
      const note = orphanedNote(result.orphaned_overrides);
      if (note) lastNotice = `${account(s)} au ${formatDate(s.statement_date)} : ${note}`;
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Action impossible : ${e.message}`;
      button.disabled = false;
    }
  };
  const replace = el("button", { type: "button", className: "primary", text: "Remplacer" });
  const keep = el("button", { type: "button", text: "Garder l'ancien" });
  replace.addEventListener("click", () => resolve(true, replace));
  keep.addEventListener("click", () => resolve(false, keep));
  return el("div", { className: "row-actions" }, replace, keep);
}

function sectionsTable(doc, ctx) {
  const head = el("thead", {}, el("tr", {},
    ["Compte", "Date", "Statut", "Opérations", "Détail"].map((h) =>
      el("th", { scope: "col", className: h === "Opérations" ? "num" : "", text: h }))));
  const body = el("tbody", {}, doc.sections.map((s) => {
    const error = el("p", { className: "error cell-error", attrs: { role: "alert" } });
    return el("tr", {},
      el("td", { text: account(s) }),
      el("td", { className: "date", text: formatDate(s.statement_date) }),
      el("td", {}, el("span", { className: `status ${s.status}`, text: STATUS[s.status] || s.status })),
      el("td", { className: "num", text: String(s.tx_count) }),
      el("td", {},
        el("span", { text: sectionNote(s) }),
        s.status === "conflict" ? conflictActions(s, ctx, error) : null,
        error));
  }));
  return el("div", { className: "table-wrap scroll-x" }, el("table", { className: "data-table" }, head, body));
}

function documentCard(doc, ctx) {
  const dated = doc.statement_date ? `Relevé du ${formatDate(doc.statement_date)}` : "Relevé sans date";
  const imported = doc.imported_at ? ` · importé le ${formatDate(doc.imported_at.slice(0, 10))}` : "";
  return section(doc.filename || `Document ${doc.id}`,
    el("p", { className: "muted", text: dated + imported }),
    doc.sections.length ? sectionsTable(doc, ctx) : emptyNote("Aucune section reconnue."));
}

function reportsCard(reports, documents) {
  const names = new Map(documents.map((d) => [d.id, d.filename]));
  const lines = [];
  for (const r of reports) {
    const name = names.get(r.document_id) || `Document ${r.document_id}`;
    if (r.error) lines.push(`${name} : ${r.error}`);
    for (const s of r.sections) {
      const status = STATUS[s.status] || s.status;
      const gap = s.status === "error" ? ` (${s.message || `écart ${money(s.gap, "balance")}`})` : "";
      const orphaned = s.orphaned_overrides > 0 ? ` — ${orphanedNote(s.orphaned_overrides)}` : "";
      lines.push(`${name} — ••${s.number_tail} au ${formatDate(s.statement_date)} : ${status}${gap}${orphaned}`);
    }
  }
  const close = el("button", { type: "button", className: "link", text: "Masquer" });
  const card = section("Résultat de la ré-analyse",
    lines.length ? el("ul", { className: "plain-list" }, lines.map((text) => el("li", { text })))
      : emptyNote("Aucun relevé à ré-analyser."),
    close);
  close.addEventListener("click", () => card.remove());
  return card;
}

function toolbar(ctx) {
  const button = el("button", { type: "button", text: "Ré-analyser tous les relevés" });
  const status = el("p", { className: "muted", attrs: { role: "status" } });
  button.addEventListener("click", async () => {
    if (!window.confirm("Relire tous les relevés conservés avec le parseur actuel ?")) return;
    button.disabled = true;
    status.className = "muted";
    status.textContent = "Ré-analyse en cours…";
    try {
      lastReports = await ctx.api.post("/reanalyse");
      await ctx.refresh();
    } catch (e) {
      status.className = "error";
      status.textContent = `Ré-analyse impossible : ${e.message}`;
      button.disabled = false;
    }
  });
  return el("div", { className: "filters" }, button, status);
}

export async function render(container, ctx) {
  const { documents } = await ctx.api.get("/tabs/releves/list");
  const reports = lastReports;
  const notice = lastNotice;
  lastReports = null;
  lastNotice = null;
  const nodes = [toolbar(ctx)];
  if (notice) nodes.push(el("p", { className: "error", attrs: { role: "status" }, text: notice }));
  if (reports) nodes.push(reportsCard(reports, documents));
  if (documents.length) nodes.push(...documents.map((d) => documentCard(d, ctx)));
  else nodes.push(emptyNote("Aucun relevé importé. Utilisez « Déposer un relevé »."));
  container.replaceChildren(...nodes);
}
