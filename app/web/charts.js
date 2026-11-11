// Graphiques Chart.js (global `Chart`, embarqué dans vendor/) : une instance par conteneur, détruite
// avant d'en créer une nouvelle ; les instances dont le canevas a quitté la page sont libérées.
// Les couleurs viennent des variables CSS, pour suivre le thème clair ou sombre.

const byHost = new WeakMap();
const live = new Set();

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** `n` couleurs de série, en boucle sur --chart-1 … --chart-8. */
export function palette(n) {
  const base = Array.from({ length: 8 }, (_, i) => cssVar(`--chart-${i + 1}`));
  return Array.from({ length: n }, (_, i) => base[i % base.length]);
}

function applyTheme() {
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.borderColor = cssVar("--border");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
}

function releaseDetached() {
  for (const chart of live) {
    if (!chart.canvas || !chart.canvas.isConnected) {
      chart.destroy();
      live.delete(chart);
    }
  }
}

/** Crée le graphique `config` dans `host` (élément de taille fixée par le CSS, classe .chart). */
export function mountChart(host, config) {
  releaseDetached();
  const previous = byHost.get(host);
  if (previous) {
    previous.destroy();
    live.delete(previous);
  }
  applyTheme();
  const canvas = document.createElement("canvas");
  host.replaceChildren(canvas);
  const chart = new Chart(canvas, {
    ...config,
    options: { responsive: true, maintainAspectRatio: false, animation: false, ...config.options },
  });
  byHost.set(host, chart);
  live.add(chart);
  return chart;
}

/** Libellé de tooltip en euros. */
export function euroTooltip(format) {
  return { callbacks: { label: (item) => `${item.dataset.label || item.label} : ${format(item.raw)}` } };
}
