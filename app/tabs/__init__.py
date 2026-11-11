"""Registre des onglets : `ENABLED_TABS` est LA liste à éditer pour ajouter ou retirer un onglet.

Chaque id correspond à un module `app/tabs/<id>.py` exposant `TAB = Tab(...)` et à un module client
`app/web/tabs/<id>.js` exportant `{ id, title, render(container, ctx) }`.
"""
from app.tabs.registry import Tab, load_tabs_from

ENABLED_TABS: list[str] = ["overview", "categories", "transactions", "epargne", "a_classer", "releves", "regles"]


def load_tabs(enabled: list[str] = ENABLED_TABS) -> list[Tab]:
    return load_tabs_from(enabled)
