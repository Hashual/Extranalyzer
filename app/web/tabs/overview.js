// Onglet « Vue d'ensemble » : contenu provisoire, implémenté avec l'onglet.
export const id = "overview";
export const title = "Vue d'ensemble";

export function render(container, ctx) {
  const p = document.createElement("p");
  p.className = "placeholder";
  p.textContent = `Période du ${ctx.period.from} au ${ctx.period.to} : vue d'ensemble à venir.`;
  container.replaceChildren(p);
}
