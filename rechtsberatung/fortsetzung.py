"""Fortsetzen eines früheren Laufs (`legal.bash --fortsetzen [LAUF-ID]`, Entscheidung Timo 2026-11-17).

Ausgaben eines früheren Laufs werden wiederverwendet, wenn Rolle, Familie, Modell, Runde und die
SHA-256-Werte ALLER Eingabedateien exakt übereinstimmen. Die Ausgabedatei muss zudem genau den
Hash haben, den der Host damals nach seiner Prüfung protokolliert hat; danach durchläuft sie die
aktuelle Rückkanal-Prüfung erneut. Ändert sich die Akte oder eine frühere Ausgabe, ändern sich die
Eingabe-Hashes der betroffenen Rollen und sie laufen neu.

Quelle ist nur `<fallordner>/ergebnis/<lauf-id>/`: das vom Host geschriebene `protokoll.jsonl`
und die Container-Ausgabeordner `NNN_rolle_familie/`. Alles wird als nicht vertrauenswürdig
gelesen (Größen, Typen, Namen, keine Symlinks).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import FAMILIEN, ROLLEN
from .logging_setup import sec_event
from .rueckkanal import MAX_AUSGABE

LAUF_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EINGABE_RE = re.compile(r"^[a-z0-9_]{1,40}\.(?:md|json)$")
_DATEI_RE = re.compile(r"^[a-z_]{1,40}\.json$")
_DOK_DATEI_RE = re.compile(r"^(dok_\d{3})\.md$")
_DOK_TOKEN_RE = re.compile(r"\bdok_(\d{3})\b")
MAX_PROTOKOLL = 20 * 1024 * 1024
MAX_EINTRAEGE = 5000


class FortsetzungFehler(ValueError):
    """Fortsetzungsquelle ungültig (Kategorie in `reason`, nie Pfade oder Inhalte)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _lies_sicher(pfad: Path, max_bytes: int) -> bytes | None:
    """Reguläre, einfach verlinkte Datei ohne Symlink lesen; sonst None."""
    try:
        st = os.lstat(pfad)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > max_bytes:
            return None
        fd = os.open(pfad, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st2 = os.fstat(fh.fileno())
        if (st2.st_ino, st2.st_dev) != (st.st_ino, st.st_dev) or not stat.S_ISREG(st2.st_mode):
            return None
        daten = fh.read(max_bytes + 1)
    return daten if len(daten) <= max_bytes else None


def _echtes_verzeichnis(pfad: Path) -> bool:
    try:
        st = os.lstat(pfad)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)


def waehle_quelle(ergebnis_basis: Path, wunsch: str, aktuelle_lauf_id: str) -> Path:
    """Quelllauf bestimmen: explizite Lauf-ID oder `letzter` (neuester anderer Lauf mit Protokoll)."""
    if wunsch != "letzter":
        if not LAUF_ID_RE.fullmatch(wunsch) or wunsch == aktuelle_lauf_id:
            raise FortsetzungFehler("lauf_id")
        quelle = ergebnis_basis / wunsch
        if not _echtes_verzeichnis(quelle):
            raise FortsetzungFehler("nicht_gefunden")
        return quelle
    kandidaten = []
    try:
        with os.scandir(ergebnis_basis) as it:
            for e in it:
                if LAUF_ID_RE.fullmatch(e.name) and e.name != aktuelle_lauf_id \
                        and e.is_dir(follow_symlinks=False) and not e.is_symlink():
                    kandidaten.append(e.name)
    except OSError as exc:
        raise FortsetzungFehler("nicht_gefunden") from exc
    for name in sorted(kandidaten, reverse=True):
        if (ergebnis_basis / name / "protokoll.jsonl").is_file():
            return ergebnis_basis / name
    raise FortsetzungFehler("nicht_gefunden")


@dataclass(frozen=True)
class _Treffer:
    ordner: Path
    ausgabe_sha256: str


def neue_und_umbenannte(alt: dict[str, str], neu: dict[str, str]) -> tuple[list[str], dict[str, str], int]:
    """Akte-Abgleich über Inhalts-Hashes.

    Rückgabe: (neue/geänderte dok-Namen des neuen Laufs, Umbenennung alt→neu für inhaltsgleiche
    Dokumente, Anzahl entfallener/geänderter Dokumente des Quelllaufs).
    """
    neu_nach_hash: dict[str, str] = {}
    for name, h in sorted(neu.items()):
        neu_nach_hash.setdefault(h, name)
    umbenennung = {a: neu_nach_hash[h] for a, h in alt.items() if h in neu_nach_hash}
    alte_hashes = set(alt.values())
    neue = sorted(name for name, h in neu.items() if h not in alte_hashes)
    entfallen = sum(1 for h in alt.values() if h not in neu_nach_hash)
    return neue, umbenennung, entfallen


def nummeriere_um(text: str, umbenennung: dict[str, str]) -> str:
    """dok-Verweise im früheren Ergebnis auf die neue Nummerierung umschreiben (ein Durchgang)."""
    def ersetze(m: re.Match[str]) -> str:
        return umbenennung.get(m.group(0), "dok_entfallen")
    return _DOK_TOKEN_RE.sub(ersetze, text)


class Fortsetzung:
    """Index der wiederverwendbaren Container-Ausgaben eines früheren Laufs."""

    def __init__(self, quelle: Path) -> None:
        self.quelle = quelle
        self.lauf_id = quelle.name
        if not LAUF_ID_RE.fullmatch(self.lauf_id):
            raise FortsetzungFehler("lauf_id")
        roh = _lies_sicher(quelle / "protokoll.jsonl", MAX_PROTOKOLL)
        if roh is None:
            raise FortsetzungFehler("protokoll")
        self._index: dict[tuple[Any, ...], _Treffer] = {}
        # Für die Grundlage bei geänderten Eingaben: alle Ausgaben je (Rolle, Familie, Modell, Runde)
        # in Aufrufreihenfolge, und die Akte des Quelllaufs als dok-Name → Inhalts-Hash.
        folge: dict[tuple[Any, ...], list[tuple[int, _Treffer]]] = {}
        self.dok_hashes: dict[str, str] = {}
        for nr_zeile, zeile in enumerate(roh.decode("utf-8", "replace").splitlines()):
            if nr_zeile >= MAX_EINTRAEGE:
                break
            try:
                d = json.loads(zeile)
            except ValueError:
                continue
            schluessel_treffer = self._eintrag(d)
            if schluessel_treffer:
                schluessel, treffer = schluessel_treffer
                self._index.setdefault(schluessel, treffer)
                folge.setdefault(schluessel[:4], []).append((d["nr"], treffer))
                for name, h in d["eingaben_sha256"].items():
                    m = _DOK_DATEI_RE.fullmatch(name)
                    if m:
                        self.dok_hashes.setdefault(m.group(1), h)
        self._folge = {k: [t for _, t in sorted(v, key=lambda x: x[0])] for k, v in folge.items()}

    def _eintrag(self, d: Any) -> tuple[tuple[Any, ...], _Treffer] | None:
        if not isinstance(d, dict) or d.get("typ") != "container" or d.get("lauf_id") != self.lauf_id:
            return None
        rolle, familie, modell = d.get("rolle"), d.get("familie"), d.get("modell")
        nr, runde, rc = d.get("nr"), d.get("runde"), d.get("rc")
        eingaben, ausgabe = d.get("eingaben_sha256"), d.get("ausgabe_sha256")
        if rolle not in ROLLEN or familie not in FAMILIEN or not isinstance(modell, str) or len(modell) > 64:
            return None
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (nr, runde, rc)):
            return None
        if rc != 0 or d.get("timed_out") or not 0 < nr < 1000 or not 0 <= runde < 100:
            return None
        if not isinstance(ausgabe, str) or not _HASH_RE.fullmatch(ausgabe) or not isinstance(eingaben, dict):
            return None
        if not eingaben or len(eingaben) > 1000 or not all(
                isinstance(k, str) and _EINGABE_RE.fullmatch(k) and isinstance(v, str) and _HASH_RE.fullmatch(v)
                for k, v in eingaben.items()):
            return None
        ordner = self.quelle / f"{nr:03d}_{rolle}_{familie}"
        return (rolle, familie, modell, runde, tuple(sorted(eingaben.items()))), _Treffer(ordner, ausgabe)

    def __len__(self) -> int:
        return len(self._index)

    def suche(self, *, rolle: str, familie: str, modell: str, runde: int, eingaben: dict[str, str],
              ausgabe: str) -> str | None:
        """Gespeicherte Ausgabe (Text) bei exakter Übereinstimmung, sonst None."""
        if not _DATEI_RE.fullmatch(ausgabe):
            return None
        t = self._index.get((rolle, familie, modell, runde, tuple(sorted(eingaben.items()))))
        return self._lade(t, ausgabe, rolle, familie)

    def grundlage(self, *, rolle: str, familie: str, modell: str, runde: int, index: int,
                  ausgabe: str) -> str | None:
        """Früheres Ergebnis derselben Rolle als Arbeitsgrundlage (Eingaben dürfen abweichen).

        `index` ist die laufende Nummer des Aufrufs dieser Rolle/Familie/Runde im neuen Lauf; gibt
        es im Quelllauf weniger, gilt der letzte.
        """
        if not _DATEI_RE.fullmatch(ausgabe):
            return None
        liste = self._folge.get((rolle, familie, modell, runde), [])
        if not liste:
            return None
        return self._lade(liste[min(index, len(liste) - 1)], ausgabe, rolle, familie)

    def _lade(self, t: _Treffer | None, ausgabe: str, rolle: str, familie: str) -> str | None:
        if t is None or not _echtes_verzeichnis(t.ordner):
            return None
        daten = _lies_sicher(t.ordner / ausgabe, MAX_AUSGABE)
        if daten is None or hashlib.sha256(daten).hexdigest() != t.ausgabe_sha256:
            sec_event("legal.resume.mismatch", "rejected", komponente="fortsetzung", rolle=rolle, familie=familie)
            return None
        try:
            return daten.decode("utf-8")
        except UnicodeDecodeError:
            return None
