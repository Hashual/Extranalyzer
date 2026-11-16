// Section « Tickets de caisse (Gmail) » de l'onglet Connexions : connexion de la boîte Gmail (lecture seule) par
// mot de passe d'application, état de la relève par libellé, relève immédiate et déconnexion.
// Le mot de passe saisi n'est jamais réaffiché : le serveur ne renvoie que l'adresse partiellement masquée.
import { el, section } from "../ui.js";

const URL = "/tabs/banque/gmail";

let lastNotice = null;      // résultat d'une action, affiché une fois après le rafraîchissement

function dateTime(iso) {
  if (!iso) return "jamais";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

/** Bouton qui lance une action, affiche son erreur et rafraîchit l'appli. */
function action(label, run, ctx, { className = "", confirm = null } = {}) {
  const button = el("button", { type: "button", className, text: label });
  const error = el("span", { className: "error", attrs: { role: "alert" } });
  button.addEventListener("click", async () => {
    if (confirm && !window.confirm(confirm)) return;
    button.disabled = true;
    error.textContent = "";
    try {
      const notice = await run();
      if (typeof notice === "string") lastNotice = notice;
      await ctx.refresh();
    } catch (e) {
      error.textContent = e.message;
      button.disabled = false;
    }
  });
  return el("span", { className: "row-actions" }, button, error);
}

function syncNotice(r) {
  const text = `${r.messages} message(s) relevé(s), ${r.files} ticket(s) enregistré(s).`;
  return r.error ? `${text} Erreur : ${r.error}` : text;
}

function help() {
  return el("ol", {},
    el("li", {}, "sur votre compte Google, activez la ",
      el("strong", { text: "validation en deux étapes" }), " (Sécurité) ;"),
    el("li", {}, "ouvrez ", el("strong", { text: "Mots de passe des applications" }),
      " et créez-en un (nom libre, par exemple « Extranalyzer ») ;"),
    el("li", { text: "collez ici votre adresse Gmail et les 16 lettres affichées par Google (les espaces sont "
      + "retirés automatiquement)." }));
}

function credentialsForm(ctx, label) {
  const address = el("input", { type: "email", placeholder: "votre.adresse@gmail.com",
    attrs: { "aria-label": "Adresse Gmail", autocomplete: "off", spellcheck: "false" } });
  const password = el("input", { type: "password", placeholder: "Mot de passe d'application (16 lettres)",
    attrs: { "aria-label": "Mot de passe d'application Gmail", autocomplete: "off", spellcheck: "false" } });
  const save = action(label, async () => {
    if (!address.value.trim() || !password.value.trim()) {
      throw new Error("renseignez l'adresse Gmail et le mot de passe d'application");
    }
    try {
      await ctx.api.put(`${URL}/credentials`, { address: address.value.trim(), app_password: password.value });
    } finally {
      password.value = "";
    }
    return "Boîte Gmail connectée : la relève démarre.";
  }, ctx, { className: "primary" });
  return el("div", { className: "filters" }, address, password, save);
}

function labelsList(labels) {
  return el("ul", { className: "plain-list" }, labels.map((l) => el("li", {},
    el("strong", { text: l.label }), ` (${l.store}) — dernière relève réussie : ${dateTime(l.last_success_at)}`,
    l.last_error ? el("span", { className: "error", attrs: { role: "alert" }, text: ` · erreur : ${l.last_error}` })
      : null)));
}

const READ_ONLY_NOTE = "Lecture seule : aucun message n'est marqué comme lu, déplacé ni supprimé. Le mot de passe "
  + "d'application est vérifié puis gardé sur votre serveur ; il n'est jamais réaffiché.";

export async function gmailSection(ctx) {
  const data = await ctx.api.get(URL);
  const notice = lastNotice;
  lastNotice = null;
  const status = notice ? el("p", { className: "muted", attrs: { role: "status" }, text: notice }) : null;
  const title = "Tickets de caisse (Gmail)";
  if (!data.configured) {
    return section(title, status,
      el("p", { text: "Relevez automatiquement les tickets de caisse (PDF) reçus par e-mail. Gmail demande un mot "
        + "de passe d'application :" }),
      help(), credentialsForm(ctx, "Connecter"),
      el("p", { className: "muted", text: READ_ONLY_NOTE }));
  }
  const server = data.source === "server";
  return section(title, status,
    el("p", { attrs: { role: "status" }, text: server
      ? `Connectée (${data.address}) : géré par la configuration du serveur.`
      : `Connectée : ${data.address}.` }),
    labelsList(data.labels),
    el("div", { className: "filters" },
      action("Relever maintenant", async () => syncNotice(await ctx.api.post(`${URL}/sync`, {})), ctx,
        { className: "primary" }),
      server ? null : action("Déconnecter", () => ctx.api.delete(`${URL}/credentials`), ctx, { className: "danger",
        confirm: "Déconnecter Gmail ? La relève s'arrête et les identifiants sont supprimés du serveur ; les tickets "
          + "déjà relevés sont conservés." })),
    server ? null : el("details", {}, el("summary", { text: "Changer de compte ou de mot de passe" }),
      credentialsForm(ctx, "Remplacer")),
    el("p", { className: "muted", text: READ_ONLY_NOTE }));
}
