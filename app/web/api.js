// Client de l'API : chemins relatifs à /api (ex. api.get("/meta")).
// Ajoute l'en-tête anti-CSRF et renvoie vers l'écran de connexion sur 401.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized = () => {};

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

function buildUrl(path, params) {
  const url = new URL("/api" + path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
    else url.searchParams.set(key, value);
  }
  return url;
}

async function handle(response, path) {
  if (response.status === 204) return null;
  if (response.ok) return response.json();
  let message = response.statusText;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // corps non JSON : on garde le statut HTTP
  }
  if (response.status === 401 && path !== "/login") onUnauthorized();
  throw new ApiError(response.status, message);
}

export const api = {
  async get(path, params) {
    const response = await fetch(buildUrl(path, params), { credentials: "same-origin" });
    return handle(response, path);
  },

  /** `path` peut porter une chaîne de requête (ex. "/x?start=…"). */
  post(path, body) {
    return send("POST", path, body);
  },

  patch(path, body) {
    return send("PATCH", path, body);
  },

  delete(path) {
    return send("DELETE", path);
  },
};

async function send(method, path, body) {
  const headers = { "X-Extranalyzer": "1" };
  let payload = body;
  if (!(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body ?? {});
  }
  const response = await fetch(buildUrl(path), { method, headers, body: payload, credentials: "same-origin" });
  return handle(response, path);
}
