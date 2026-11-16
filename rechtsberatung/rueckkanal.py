"""Rückkanal Container → Host (TM-AC-11, TM-AC-15).

Der Host liest nur erwartete Dateinamen per `lstat`/`O_NOFOLLOW`: reguläre Datei,
kein Symlink, `nlink == 1`, Größe ≤ 1 MiB, UTF-8. Strukturierte Ausgaben sind
JSON und werden gegen handgeschriebene Schemata geprüft (Typen, Enums, Längen,
endliche Noten 0–10, ISO-Datum). Jeder Verstoß bedeutet „Rolle fehlgeschlagen“.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable

from .logging_setup import sec_event

MAX_AUSGABE = 1024 * 1024
_DATEI_RE = re.compile(r"^[a-z_]{1,40}\.(?:json|md)$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")

STATUS_EINWAND = ("übernommen", "abgelehnt")


class RolleFehlgeschlagen(RuntimeError):
    """Eine Rolle hat keine gültige Ausgabe geliefert (fail-closed)."""

    def __init__(self, reason: str, rolle: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.rolle = rolle


class SchemaFehler(ValueError):
    """Ausgabe verletzt das erwartete Schema."""


def _abweisen(reason: str, rolle: str) -> RolleFehlgeschlagen:
    sec_event("legal.output.rejected", "rejected", komponente="rueckkanal", reason=reason, rolle=rolle)
    return RolleFehlgeschlagen(reason, rolle)


def lies_ausgabe(out_dir: Path, name: str, *, rolle: str = "", max_bytes: int = MAX_AUSGABE) -> str:
    """Erwartete Ausgabedatei sicher lesen; Symlink, Hardlink, Übergröße und Nicht-UTF-8 abweisen."""
    if not _DATEI_RE.fullmatch(name):
        raise ValueError("unerwarteter Ausgabedateiname")
    pfad = Path(out_dir) / name
    try:
        st = os.lstat(pfad)
    except FileNotFoundError as exc:
        raise _abweisen("schema", rolle) from exc
    if stat.S_ISLNK(st.st_mode):
        raise _abweisen("symlink", rolle)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise _abweisen("unexpected", rolle)
    if st.st_size > max_bytes:
        raise _abweisen("size", rolle)
    try:
        fd = os.open(pfad, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise _abweisen("symlink", rolle) from exc
    with os.fdopen(fd, "rb") as fh:
        st2 = os.fstat(fh.fileno())
        # Gegen Austausch zwischen lstat und open: gleiche Datei, weiterhin regulär und einfach verlinkt.
        if (st2.st_ino, st2.st_dev) != (st.st_ino, st.st_dev) or not stat.S_ISREG(st2.st_mode) \
                or st2.st_nlink != 1:
            raise _abweisen("unexpected", rolle)
        daten = fh.read(max_bytes + 1)
    if len(daten) > max_bytes:
        raise _abweisen("size", rolle)
    if b"\x00" in daten:
        raise _abweisen("schema", rolle)
    try:
        return daten.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _abweisen("schema", rolle) from exc


def _keine_konstanten(name: str) -> Any:
    raise SchemaFehler("NaN/Infinity nicht erlaubt")


def lies_json(out_dir: Path, name: str, validator: Callable[[Any], Any], *, rolle: str = "") -> Any:
    """JSON-Ausgabe lesen und validieren; Rückgabe ist die validierte Struktur."""
    text = lies_ausgabe(out_dir, name, rolle=rolle)
    try:
        daten = json.loads(text, parse_constant=_keine_konstanten)
    except (ValueError, RecursionError) as exc:   # inkl. JSONDecodeError, SchemaFehler, > 4300 Ziffern
        raise _abweisen("schema", rolle) from exc
    try:
        return validator(daten)
    except NotenFehler as exc:
        sec_event("legal.score.invalid", "rejected", komponente="rueckkanal", rolle=rolle)
        raise _abweisen("schema", rolle) from exc
    except SchemaFehler as exc:
        raise _abweisen("schema", rolle) from exc


class NotenFehler(SchemaFehler):
    """Note fehlt, ist nicht endlich oder liegt außerhalb 0–10 (TM-AC-15)."""


# ---------------------------------------------------------------- Bausteine

def _obj(d: Any, pflicht: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise SchemaFehler("Objekt erwartet")
    fehlend = [k for k in pflicht if k not in d]
    if fehlend:
        raise SchemaFehler("Pflichtfeld fehlt")
    if set(d) - set(pflicht) - set(optional):
        raise SchemaFehler("unbekanntes Feld")
    return d


def _str(v: Any, max_len: int, *, min_len: int = 1) -> str:
    if not isinstance(v, str) or not min_len <= len(v) <= max_len:
        raise SchemaFehler("Zeichenkette ungültig")
    return v


def _liste(v: Any, max_items: int, item: Callable[[Any], Any]) -> list[Any]:
    if not isinstance(v, list) or len(v) > max_items:
        raise SchemaFehler("Liste ungültig")
    return [item(x) for x in v]


def _enum(v: Any, werte: tuple[str, ...]) -> str:
    if v not in werte:
        raise SchemaFehler("Enum-Wert ungültig")
    return str(v)


def _int(v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise SchemaFehler("Ganzzahl ungültig")
    return v


def note(v: Any) -> float:
    """Note als endliche Zahl in 0–10; bool, Strings, NaN, Inf und Bereichsfehler werden abgewiesen."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise NotenFehler("Note fehlt oder ist keine Zahl")
    f = float(v)
    if not math.isfinite(f) or not 0.0 <= f <= 10.0:
        raise NotenFehler("Note außerhalb 0–10")
    return f


def datum(v: Any) -> datetime.date:
    """ISO-Datum `YYYY-MM-DD`."""
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise SchemaFehler("Datum ungültig")
    try:
        return datetime.date.fromisoformat(v)
    except ValueError as exc:
        raise SchemaFehler("Datum ungültig") from exc


def _id(v: Any) -> str:
    if not isinstance(v, str) or not _ID_RE.fullmatch(v):
        raise SchemaFehler("ID ungültig")
    return v


def _text(v: Any) -> str:
    return _str(v, 4000)


def _rueckfragen(d: dict[str, Any]) -> list[str]:
    return _liste(d.get("rueckfragen", []), 30, lambda x: _str(x, 1000))


def _einwand(e: Any) -> dict[str, str]:
    e = _obj(e, ("id", "text"))
    return {"id": _id(e["id"]), "text": _text(e["text"])}


# ---------------------------------------------------------------- Rollen-Schemata

def schema_sachverhalt(d: Any) -> dict[str, Any]:
    d = _obj(d, ("tatsachen", "chronologie", "luecken"), ("rueckfragen",))

    def tatsache(t: Any) -> dict[str, Any]:
        t = _obj(t, ("id", "text", "quellen"))
        return {"id": _id(t["id"]), "text": _text(t["text"]),
                "quellen": _liste(t["quellen"], 10, lambda q: _str(q, 40))}

    def ereignis(e: Any) -> dict[str, Any]:
        e = _obj(e, ("datum", "ereignis", "quellen"))
        return {"datum": datum(e["datum"]).isoformat(), "ereignis": _text(e["ereignis"]),
                "quellen": _liste(e["quellen"], 10, lambda q: _str(q, 40))}

    return {
        "tatsachen": _liste(d["tatsachen"], 500, tatsache),
        "chronologie": _liste(d["chronologie"], 500, ereignis),
        "luecken": _liste(d["luecken"], 200, _text),
        "rueckfragen": _rueckfragen(d),
    }


def schema_rechtsfragen(d: Any) -> dict[str, Any]:
    d = _obj(d, ("fragen", "normen"), ("rueckfragen",))
    return {"fragen": _liste(d["fragen"], 30, lambda x: _str(x, 1000)),
            "normen": _liste(d["normen"], 50, lambda x: _str(x, 200)),
            "rueckfragen": _rueckfragen(d)}


def schema_register(d: Any) -> dict[str, Any]:
    d = _obj(d, ("eintraege",), ("rueckfragen",))

    def eintrag(e: Any) -> dict[str, Any]:
        e = _obj(e, ("id", "gericht", "az", "datum", "url"), ("ecli", "zitate", "leitsatz"))
        return {
            "id": _id(e["id"]), "gericht": _str(e["gericht"], 120), "az": _str(e["az"], 80),
            "datum": _str(e["datum"], 10, min_len=10), "url": _str(e["url"], 2000),
            "ecli": _str(e.get("ecli", "") or "-", 120),
            "zitate": _liste(e.get("zitate", []), 20, lambda z: _str(z, 120)),
            "leitsatz": _str(e.get("leitsatz", "") or "-", 2000),
        }

    eintraege = _liste(d["eintraege"], 100, eintrag)
    if len({e["id"] for e in eintraege}) != len(eintraege):
        raise SchemaFehler("doppelte Register-ID")
    return {"eintraege": eintraege, "rueckfragen": _rueckfragen(d)}


def schema_bestaetigung(d: Any) -> dict[str, Any]:
    d = _obj(d, ("bestaetigungen",), ("rueckfragen",))

    def b(x: Any) -> dict[str, Any]:
        x = _obj(x, ("id", "abgerufen", "gericht", "az", "datum"))
        if not isinstance(x["abgerufen"], bool):
            raise SchemaFehler("abgerufen muss bool sein")
        return {"id": _id(x["id"]), "abgerufen": x["abgerufen"], "gericht": _str(x["gericht"], 120, min_len=0),
                "az": _str(x["az"], 80, min_len=0), "datum": _str(x["datum"], 10, min_len=0)}

    return {"bestaetigungen": _liste(d["bestaetigungen"], 100, b), "rueckfragen": _rueckfragen(d)}


def schema_entwurf(d: Any) -> dict[str, Any]:
    d = _obj(d, ("hauptteil",), ("rueckfragen",))
    return {"hauptteil": _str(d["hauptteil"], 200_000), "rueckfragen": _rueckfragen(d)}


def schema_review(d: Any) -> dict[str, Any]:
    """Cross-Review, Richter und Gegenanwalt: Note, Begründung, Einwände."""
    if isinstance(d, dict) and "note" not in d:
        raise NotenFehler("Note fehlt")
    d = _obj(d, ("note", "begruendung", "einwaende"), ("rueckfragen",))
    return {"note": note(d["note"]), "begruendung": _text(d["begruendung"]),
            "einwaende": _liste(d["einwaende"], 50, _einwand), "rueckfragen": _rueckfragen(d)}


def schema_ueberarbeitung(d: Any) -> dict[str, Any]:
    d = _obj(d, ("hauptteil", "antworten"), ("rueckfragen",))

    def antwort(a: Any) -> dict[str, str]:
        a = _obj(a, ("id", "status", "begruendung"))
        return {"id": _id(a["id"]), "status": _enum(a["status"], STATUS_EINWAND),
                "begruendung": _str(a["begruendung"], 4000, min_len=0)}

    return {"hauptteil": _str(d["hauptteil"], 200_000), "antworten": _liste(d["antworten"], 200, antwort),
            "rueckfragen": _rueckfragen(d)}


def schema_persona(d: Any) -> dict[str, Any]:
    d = _obj(d, ("hinweise",), ("rueckfragen",))
    return {"hinweise": _liste(d["hinweise"], 20, _text), "rueckfragen": _rueckfragen(d)}


FRIST_ARTEN = ("Frist", "Verjährung", "Zuständigkeit", "Form", "Kosten", "Sonstiges")


def schema_fristen(d: Any) -> dict[str, Any]:
    d = _obj(d, ("fristen", "risiken"), ("rueckfragen",))

    def frist(f: Any) -> dict[str, Any]:
        f = _obj(f, ("bezeichnung", "datum", "quelle"), ("art",))
        return {"bezeichnung": _str(f["bezeichnung"], 300), "datum": datum(f["datum"]).isoformat(),
                "quelle": _str(f["quelle"], 200, min_len=0), "art": _enum(f.get("art", "Frist"), FRIST_ARTEN)}

    def risiko(r: Any) -> dict[str, str]:
        r = _obj(r, ("art", "text"))
        return {"art": _enum(r["art"], FRIST_ARTEN), "text": _text(r["text"])}

    return {"fristen": _liste(d["fristen"], 100, frist), "risiken": _liste(d["risiken"], 100, risiko),
            "rueckfragen": _rueckfragen(d)}


def schema_vergleich(d: Any) -> dict[str, Any]:
    d = _obj(d, ("spanne_min", "spanne_max", "waehrung", "begruendung"), ("rueckfragen",))
    werte = []
    for k in ("spanne_min", "spanne_max"):
        v = d[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) \
                or not 0 <= float(v) <= 1e12:
            raise SchemaFehler("Betrag ungültig")
        werte.append(float(v))
    if werte[0] > werte[1]:
        raise SchemaFehler("Spanne ungültig")
    return {"spanne_min": werte[0], "spanne_max": werte[1], "waehrung": _enum(d["waehrung"], ("EUR",)),
            "begruendung": _str(d["begruendung"], 8000), "rueckfragen": _rueckfragen(d)}


def schema_groesse(d: Any) -> dict[str, Any]:
    d = _obj(d, ("vorschlaege",), ("rueckfragen",))

    def v(x: Any) -> dict[str, Any]:
        x = _obj(x, ("abschnitt", "woerter_vorher", "woerter_nachher", "vorschlag"))
        vorher = _int(x["woerter_vorher"], 0, 1_000_000)
        nachher = _int(x["woerter_nachher"], 0, 1_000_000)
        return {"abschnitt": _str(x["abschnitt"], 300), "woerter_vorher": vorher, "woerter_nachher": nachher,
                "vorschlag": _text(x["vorschlag"])}

    return {"vorschlaege": _liste(d["vorschlaege"], 100, v), "rueckfragen": _rueckfragen(d)}


def schema_coldread(d: Any) -> dict[str, Any]:
    d = _obj(d, ("befunde",), ("rueckfragen",))

    def b(x: Any) -> dict[str, str]:
        x = _obj(x, ("aussage", "grund"))
        return {"aussage": _str(x["aussage"], 2000), "grund": _str(x["grund"], 2000)}

    return {"befunde": _liste(d["befunde"], 100, b), "rueckfragen": _rueckfragen(d)}
