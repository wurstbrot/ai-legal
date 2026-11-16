"""Test-Hilfen: `FakeRunner` (schreibt schemakonforme Rollen-Ausgaben) und Fallordner-Fabrik.

Docker wird nie aufgerufen; der Runner wird dem Conductor injiziert.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from rechtsberatung.container import Aufruf, RunErgebnis

AUSGABEDATEI = {
    "sachverhalt": "sachverhalt.json", "rechtsfragen": "rechtsfragen.json", "researcher": "register.json",
    "registercheck": "bestaetigung.json", "anwalt": "entwurf.json", "review": "review.json",
    "ueberarbeitung": "ueberarbeitung.json", "richter": "bewertung.json", "gegenanwalt": "bewertung.json",
    "persona_klaeger": "persona.json", "persona_beklagter": "persona.json", "fristen": "fristen.json",
    "stratege": "vergleich.json", "groesse": "groesse.json", "coldread": "befunde.json",
}

KANARIE = "KANARIE-7f3a9c-AKTENINHALT"
URL_R1 = "https://dejure.org/dienste/vernetzung/rechtsprechung?Az=VIII%20ZR%20123/24"
# Simulierter Inhalt der vom Bestätiger abgerufenen Seiten (Gericht, Az., Datum).
SEITEN = {URL_R1: {"gericht": "BGH", "az": "VIII ZR 123/24", "datum": "2024-05-01"}}


def _lies(aufruf: Aufruf, name: str) -> str:
    return (aufruf.in_dir / name).read_text(encoding="utf-8")


def standard_ausgabe(a: Aufruf) -> Any:
    """Schemakonforme Standardausgabe je Rolle."""
    r = a.rolle
    if r == "sachverhalt":
        return {"tatsachen": [{"id": "T1", "text": "Mietvertrag geschlossen", "quellen": ["dok_001"]}],
                "chronologie": [{"datum": "2026-01-01", "ereignis": "Vertragsschluss", "quellen": ["dok_001"]}],
                "luecken": []}
    if r == "rechtsfragen":
        return {"fragen": ["Mietminderung bei Schimmel, Beweislast"], "normen": ["§ 536 BGB"]}
    if r == "researcher":
        return {"eintraege": [{"id": "R1", "gericht": "BGH", "az": "VIII ZR 123/24", "datum": "2024-05-01",
                               "url": URL_R1,
                               "zitate": ["NJW 2024, 123"]}]}
    if r == "registercheck":
        # Blindbestätigung: Eingabe hat nur id/url; die „abgerufene Seite“ simuliert SEITEN.
        eintraege = json.loads(_lies(a, "register_eintraege.json"))
        return {"bestaetigungen": [{"id": e["id"], "abgerufen": True, **SEITEN[e["url"]]} for e in eintraege
                                   if e["url"] in SEITEN]}
    if r == "anwalt":
        m = re.search(r"Ergebnis-Art laut Auftrag: (.+)", a.prompt)
        art = m.group(1).strip() if m else "?"
        return {"hauptteil": f"Entwurf {a.familie} für {art}. Nach BGH VIII ZR 123/24 ist die Miete gemindert "
                             "(dok_001)."}
    if r in ("review", "richter", "gegenanwalt"):
        return {"note": 7 if a.familie == "codex" else 6, "begruendung": "nachvollziehbar",
                "einwaende": [{"id": "E1", "text": f"Einwand {r}: Beweislast ergänzen"}]}
    if r == "ueberarbeitung":
        einwaende = json.loads(_lies(a, "einwaende.json"))
        return {"hauptteil": _lies(a, "entwurf.md") + " Überarbeitet.",
                "antworten": [{"id": e["id"], "status": "übernommen", "begruendung": "eingearbeitet"}
                              for e in einwaende]}
    if r in ("persona_klaeger", "persona_beklagter"):
        return {"hinweise": [f"Hinweis {r}"]}
    if r == "fristen":
        return {"fristen": [{"bezeichnung": "Berufungsfrist", "datum": "2026-12-01", "quelle": "dok_001"}],
                "risiken": [{"art": "Kosten", "text": "Kostenrisiko gering"}]}
    if r == "stratege":
        return {"spanne_min": 10000, "spanne_max": 12000, "waehrung": "EUR", "begruendung": "Richter 50:50"}
    if r == "groesse":
        return {"vorschlaege": [{"abschnitt": "Einleitung", "woerter_vorher": 20, "woerter_nachher": 10,
                                 "vorschlag": "kürzen"}]}
    if r == "coldread":
        return {"befunde": []}
    raise AssertionError(r)


class FakeRunner:
    """Injizierter Runner: protokolliert Aufrufe und schreibt je Rolle eine Ausgabe nach `out_dir`.

    `overrides[rolle]` ist eine Funktion `(aufruf, standard) -> Ausgabe`. Ist die Ausgabe ein
    `str`, wird sie roh geschrieben; `None` schreibt nichts; ein `RunErgebnis` wird direkt
    zurückgegeben (z. B. Timeout).
    """

    def __init__(self, overrides: dict[str, Callable[[Aufruf, Any], Any]] | None = None) -> None:
        self.overrides = overrides or {}
        self.aufrufe: list[Aufruf] = []
        self.eingaben: list[tuple[str, str, list[str]]] = []
        self._lock = threading.Lock()

    def __call__(self, a: Aufruf) -> RunErgebnis:
        with self._lock:
            self.aufrufe.append(a)
            self.eingaben.append((a.rolle, a.familie, sorted(os.listdir(a.in_dir))))
        std = standard_ausgabe(a)
        aus = self.overrides[a.rolle](a, std) if a.rolle in self.overrides else std
        if isinstance(aus, RunErgebnis):
            return aus
        if aus is not None:
            ziel = a.out_dir / AUSGABEDATEI[a.rolle]
            ziel.write_text(aus if isinstance(aus, str) else json.dumps(aus, ensure_ascii=False), encoding="utf-8")
        return RunErgebnis(rc=0)

    def rollen(self) -> list[tuple[str, str]]:
        return [(a.rolle, a.familie) for a in self.aufrufe]


def erstelle_fall(basis: Path, *, anweisung: str = "Ergebnis: Einschätzung\nSeite: Kläger\n",
                  dateien: dict[str, str | bytes] | None = None) -> Path:
    """Fallordner mit `anweisung.md` und `fall/` anlegen."""
    root = basis / "fallordner"
    (root / "fall").mkdir(parents=True)
    (root / "anweisung.md").write_text(anweisung, encoding="utf-8")
    if dateien is None:
        dateien = {"01_mail.txt": f"<NAME_1> schreibt am 2026-01-01: Mietvertrag. {KANARIE}"}
    for name, inhalt in dateien.items():
        p = root / "fall" / name
        if isinstance(inhalt, bytes):
            p.write_bytes(inhalt)
        else:
            p.write_text(inhalt, encoding="utf-8")
    return root
