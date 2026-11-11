"""Contrat d'un onglet et chargement des modules `app.tabs.<id>`."""
import importlib
from dataclasses import dataclass

from fastapi import APIRouter


@dataclass(frozen=True)
class Tab:
    id: str
    title: str
    order: int
    router: APIRouter       # monté sous /api/tabs/<id>/


def load_tab(tab_id: str) -> Tab:
    module_name = f"app.tabs.{tab_id}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:      # une dépendance du module manque : erreur d'origine
            raise
        raise RuntimeError(f"onglet activé mais module introuvable : {tab_id}") from exc
    tab = getattr(module, "TAB", None)
    if not isinstance(tab, Tab) or tab.id != tab_id:
        raise RuntimeError(f"le module {module_name} doit exposer TAB = Tab(id={tab_id!r}, ...)")
    return tab


def load_tabs_from(enabled: list[str]) -> list[Tab]:
    if len(set(enabled)) != len(enabled):
        raise RuntimeError(f"onglet listé deux fois : {enabled}")
    return sorted((load_tab(tab_id) for tab_id in enabled), key=lambda t: t.order)
