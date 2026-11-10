"""Onglet « Vue d'ensemble » (routes ajoutées avec l'onglet lui-même)."""
from fastapi import APIRouter

from app.tabs.registry import Tab

router = APIRouter()

TAB = Tab("overview", "Vue d'ensemble", 10, router)
