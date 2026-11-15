// Retour de la banque : la page arrive sans cookie de session (SameSite=Strict lors d'une navigation venue de la
// banque). Elle renvoie code et state à l'API depuis l'origine de l'appli, où le cookie est cette fois envoyé, puis
// retire ces paramètres de l'adresse et ouvre l'onglet Banque.
const status = document.getElementById("callback-status");
const params = new URLSearchParams(window.location.search);
const body = {};
for (const key of ["code", "state", "error", "error_description"]) {
  if (params.get(key)) body[key] = params.get(key);
}
history.replaceState(null, "", window.location.pathname);

async function finish() {
  if (!body.state) {
    status.textContent = "Adresse de retour incomplète : relancez la connexion depuis l'onglet Banque.";
    return;
  }
  const response = await fetch("/api/tabs/banque/session", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Extranalyzer": "1" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (response.ok) {
    window.location.replace("/#tab=banque");
    return;
  }
  let detail = response.statusText;
  try {
    detail = (await response.json()).detail || detail;
  } catch {
    // corps non JSON
  }
  status.textContent = response.status === 401
    ? "Session Extranalyzer expirée : connectez-vous puis relancez la connexion bancaire depuis l'onglet Banque."
    : `Connexion bancaire impossible : ${detail}`;
}

finish().catch((error) => {
  status.textContent = `Connexion bancaire impossible : ${error.message}`;
});
