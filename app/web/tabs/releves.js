// Onglet « Relevés » : relevés importés, statut de réconciliation de chaque section, conflits à trancher,
// suppression d'un relevé et ré-analyse de tous les relevés (routes du cœur /sections/{id}/resolve,
// DELETE /documents/{id} et /reanalyse).
import { confirmDialog, el, emptyNote, formatDate, money, section } from "../ui.js";

export const id = "releves";
export const title = "Relevés";

const STATUS = {
  ok: "Réconcilié",
  error: "En erreur",
  conflict: "Conflit",
  superseded: "Écarté",
};

let lastReports = null;      // résultat de la dernière ré-analyse, affiché une fois après le rafraîchissement
let lastNotice = null;       // compte rendu d'une action ({ kind, lines }), affiché une fois

function orphanedNote(n) {
  return n > 0 ? `${n} reclassement(s) manuel(s) sans opération correspondante (à refaire)` : "";
}

function account(s) {
  return `${s.account_label} ••${s.number_tail}`;
}

function sectionNote(s) {
  if (s.status === "error") return s.message || "écart de réconciliation";
  if (s.status === "conflict" && !s.other_ok) {
    return "Aucune version de ce relevé n'est retenue (la précédente a été supprimée).";
  }
  if (s.status === "conflict") return "Diffère d'une version déjà importée de ce relevé.";
  if (s.status === "superseded") return "Version non retenue : aucune opération importée.";
  return "";
}

function conflictActions(s, ctx, error) {
  const resolve = async (accept, button) => {
    const where = `${account(s)} au ${formatDate(s.statement_date)}`;
    const question = !s.other_ok
      ? (accept ? `Importer les opérations de cette version de ${where} ?` : `Écarter cette version de ${where} ?`)
      : accept
        ? `Remplacer les opérations de ${where} par cette nouvelle version ?`
        : `Garder la version déjà importée de ${where} et écarter celle-ci ?`;
    if (!(await confirmDialog(question))) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const result = await ctx.api.post(`/sections/${s.id}/resolve`, { accept });
      const note = orphanedNote(result.orphaned_overrides);
      if (note) lastNotice = { kind: "error", lines: [`${account(s)} au ${formatDate(s.statement_date)} : ${note}`] };
      await ctx.refresh();
    } catch (e) {
      error.textContent = `Action impossible : ${e.message}`;
      button.disabled = false;
    }
  };
  const replace = el("button", { type: "button", className: "primary", text: s.other_ok ? "Remplacer" : "Importer" });
  const keep = el("button", { type: "button", text: s.other_ok ? "Garder l'ancien" : "Écarter" });
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

function plural(n, one, many) {
  return `${n} ${n > 1 ? many : one}`;
}

function deletionLines(name, result) {
  const lines = [`« ${name} » supprimé : ${plural(result.deleted_transactions, "opération retirée", "opérations retirées")}.`];
  if (result.reclassified_mirrors > 0) {
    lines.push(result.reclassified_mirrors > 1
      ? `${result.reclassified_mirrors} virements internes d'autres relevés ont perdu leur contrepartie et ont été reclassés.`
      : "1 virement interne d'un autre relevé a perdu sa contrepartie et a été reclassé.");
  }
  for (const v of result.pending_versions) {
    lines.push(`Une autre version de ••${v.number_tail} au ${formatDate(v.statement_date)} est à trancher `
      + "(rien n'a été importé à sa place).");
  }
  return lines;
}

function deleteAction(doc, ctx) {
  const name = doc.filename || `Document ${doc.id}`;
  const count = doc.sections.reduce((n, s) => n + s.tx_count, 0);
  const button = el("button", { type: "button", className: "danger", text: "Supprimer" });
  const error = el("p", { className: "error", attrs: { role: "alert" } });
  button.addEventListener("click", async () => {
    const question = `Supprimer le relevé « ${name} » et ${plural(count, "opération", "opérations")} ?

`
      + "Le PDF conservé est effacé. Vos reclassements manuels sont gardés : ils se ré-appliqueront "
      + "si vous réimportez ce relevé.";
    if (!(await confirmDialog(question))) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const result = await ctx.api.delete(`/documents/${doc.id}`);
      lastNotice = { kind: result.pending_versions.length ? "error" : "muted", lines: deletionLines(name, result) };
      await ctx.refresh();     // pastille « À classer », comptes et plage de dates, puis cet onglet
    } catch (e) {
      error.textContent = `Suppression impossible : ${e.message}`;
      button.disabled = false;
    }
  });
  return el("div", { className: "row-actions" }, button, error);
}

function documentCard(doc, ctx) {
  const dated = doc.statement_date ? `Relevé du ${formatDate(doc.statement_date)}` : "Relevé sans date";
  const imported = doc.imported_at ? ` · importé le ${formatDate(doc.imported_at.slice(0, 10))}` : "";
  return section(doc.filename || `Document ${doc.id}`,
    el("p", { className: "muted", text: dated + imported }),
    doc.sections.length ? sectionsTable(doc, ctx) : emptyNote("Aucune section reconnue."),
    deleteAction(doc, ctx));
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
    if (!(await confirmDialog("Relire tous les relevés conservés avec le parseur actuel ?", { confirmLabel: "Ré-analyser" }))) return;
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
  if (notice) {
    nodes.push(el("div", { className: notice.kind, attrs: { role: "status" } },
      notice.lines.map((text) => el("p", { text }))));
  }
  if (reports) nodes.push(reportsCard(reports, documents));
  if (documents.length) nodes.push(...documents.map((d) => documentCard(d, ctx)));
  else nodes.push(emptyNote("Aucun relevé importé. Utilisez « Déposer un relevé »."));
  container.replaceChildren(...nodes);
}
