"""Synchro planifiée dans le processus : un thread démon appelle `tick` toutes les `every_s` secondes."""
import threading
from collections.abc import Callable

FIRST_DELAY_S = 300      # première passe quelques minutes après le démarrage


class Scheduler:
    def __init__(self, tick: Callable[[], None], every_s: float, first_delay_s: float = FIRST_DELAY_S,
                 name: str = "synchro-bancaire"):
        if every_s <= 0:
            raise ValueError("période de synchro nulle ou négative")
        self._tick, self._every, self._first, self._name = tick, every_s, first_delay_s, name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        delay = self._first
        while not self._stop.wait(delay):
            self._tick()
            delay = self._every

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
