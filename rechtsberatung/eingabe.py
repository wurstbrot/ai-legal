"""Eingangsprüfung des Fallordners (CAP-1, CAP-9, TM-AC-1, -2, -4, -8, -18).

Alle Prüfungen laufen vor jedem Modellaufruf. Verstöße führen zu `EingabeFehler`
bzw. `MountUnsicher` (Exit 2). Logs nennen nur Kategorie und Datei-Index, nie
Dateinamen oder Inhalte der Akte.

Die Akte sind alle Dateien im Fall-Ordner und seinen Unterordnern (rekursiv,
sortiert nach relativem Pfad), ausgenommen auf oberster Ebene `anweisung.md`,
das Ausgabeverzeichnis `ergebnis/` und die Hold-Datei `HOLD` (SPEC 2026-11-16).
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .config import VERBOTENE_MOUNT_NAMEN
from .logging_setup import sec_event

MAX_DATEI = 2 * 1024 * 1024
MAX_DATEIEN = 200
MAX_GESAMT = 20 * 1024 * 1024
# Obergrenze besuchter Einträge (Dateien und Ordner) beim rekursiven Einlesen; schützt vor
# versehentlichem Start in einem großen Verzeichnisbaum (z. B. ~).
MAX_EINTRAEGE = 2000
# Auf oberster Ebene des Fall-Ordners keine Akte: Auftrag, eigene Ausgaben, Hold-Datei.
AUSGENOMMEN_OBEN = ("anweisung.md", "ergebnis", "HOLD")
MAX_ANWEISUNG = 64 * 1024
ENDUNGEN = (".txt", ".md")

WORTLIMIT_MIN, WORTLIMIT_MAX = 50, 20000
ERGEBNIS_DEFAULT = "Einschätzung"


class EingabeFehler(ValueError):
    """Ungültiger Fallordner; `reason` ist eine Kategorie ohne Inhalte."""

    def __init__(self, reason: str, file_index: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.file_index = file_index


class MountUnsicher(RuntimeError):
    """Mapping-Datei, verbotener Name oder Symlink im Mount (TM-AC-4)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Fallordner:
    """Geprüfter Fallordner mit nach relativem Pfad sortierter Akten-Dateiliste."""

    root: Path
    anweisung: Path
    dateien: tuple[Path, ...]
    # Inhalte, wie sie das Gate gelesen und geprüft hat; nur diese gehen weiter (kein zweites Lesen).
    texte: tuple[str, ...] = ()


@dataclass(frozen=True)
class Anweisung:
    """Auftrag aus `anweisung.md`; Steuerung nur über die Schlüsselzeilen (E-7)."""

    text: str
    ergebnis: str = ERGEBNIS_DEFAULT
    seite: str | None = None
    wortlimit: int | None = None
    personas: bool = False
    vergleich: bool = False
    gesetzte_schluessel: tuple[str, ...] = field(default=())


# Hinweise für Timo je Ablehnungsgrund; enthalten nur feste Namen, keine Pfade oder Inhalte.
HINWEISE = {
    "path": "Fallordner existiert nicht oder ist kein Verzeichnis.",
    "missing_anweisung": "Im Fallordner fehlt die Datei 'anweisung.md' (genau so: klein, Endung .md).",
    "symlink": "Symbolische Links sind nicht erlaubt (anweisung.md oder ein Eintrag im Fallordner/Unterordner).",
    "type": "anweisung.md und alle Akten-Einträge müssen normale Dateien oder Ordner sein.",
    "hardlink": "Hardlinks sind nicht erlaubt.",
    "hidden": "Versteckte Dateien/Ordner (Name beginnt mit '.') sind nicht erlaubt, auch in Unterordnern.",
    "extension": "Nur .txt- und .md-Dateien erlaubt, auch in Unterordnern (außer anweisung.md, ergebnis/, HOLD).",
    "size": "Zu groß: max. 2 MiB je Datei, 200 Dateien, 20 MiB, 2000 Einträge, anweisung.md 64 KiB.",
    "encoding": "Datei ist kein gültiges UTF-8 oder enthält NUL-Zeichen.",
    "name": "Datei- oder Ordnername ist kein gültiges UTF-8.",
    "empty": "Im Fallordner liegen außer anweisung.md keine Akten-Dateien (.txt/.md).",
    "unreadable": "Datei nicht lesbar (Rechte prüfen).",
    "instruction_duplicate": "Eine Schlüsselzeile (Ergebnis/Seite/Wortlimit/Personas/Vergleich) steht mehrfach in anweisung.md.",
    "instruction_value": "Ungültiger Wert in anweisung.md: Ergebnis: <Text ≤ 80 Zeichen>, Seite: Kläger|Beklagter, "
                         "Wortlimit: 50–20000, Personas: an|aus, Vergleich: ja|nein.",
    "mapping": "Mapping-Datei (z. B. mapping.md) im Fallordner oder Unterordner; darf nie an die Modelle.",
    "cli_config": "Im Mount liegt eine CLI-Konfiguration (z. B. CLAUDE.md, .claude, AGENTS.md).",
}


def hinweis(reason: str, file_index: int | None = None) -> str:
    """Verständlicher Hinweis zum Ablehnungsgrund (ohne Pfade/Inhalte)."""
    text = HINWEISE.get(reason, f"Grund: {reason}")
    if file_index is not None:
        text += f" (Eintrag Nr. {file_index}, nach relativem Pfad sortiert)"
    return text


def _abweisen(reason: str, index: int | None = None) -> EingabeFehler:
    felder = {"reason": reason}
    if index is not None:
        felder["file_index"] = str(index)
    sec_event("legal.input.rejected", "rejected", komponente="eingabe", **felder)
    return EingabeFehler(reason, index)


def _lies_text(pfad: Path, max_bytes: int, index: int | None) -> str:
    """Reguläre Datei ohne Symlink-Folgen lesen und als UTF-8 ohne NUL prüfen.

    `O_NONBLOCK`, damit eine nachträglich untergeschobene FIFO nicht blockiert; Typ und
    Hardlink-Zahl werden am geöffneten Deskriptor geprüft (kein Wechsel nach `lstat`).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(pfad, flags)
    except OSError as exc:
        raise _abweisen("unreadable", index) from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _abweisen("type", index)
        if st.st_nlink > 1:
            raise _abweisen("hardlink", index)
        if st.st_size > max_bytes:
            raise _abweisen("size", index)
        daten = fh.read(max_bytes + 1)
    if len(daten) > max_bytes:
        raise _abweisen("size", index)
    if b"\x00" in daten:
        raise _abweisen("encoding", index)
    try:
        return daten.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _abweisen("encoding", index) from exc


def pruefe_mapping(verzeichnis: Path, mapping_namen: tuple[str, ...]) -> None:
    """Mount-Gate (TM-AC-4) für Container-Eingaben: keine Mapping-Datei, kein verbotener Name, kein Symlink.

    Prüft rekursiv per `lstat`, ohne Symlinks zu folgen. Für den Fallordner selbst übernimmt
    `pruefe_fallordner` das Mapping-Gate (dort wird `CLAUDE.md` als Akte akzeptiert und unter
    neutralem Namen kopiert, TM-AC-2).
    """
    verboten_map = {n.casefold() for n in mapping_namen}
    verboten_cli = {n.casefold() for n in VERBOTENE_MOUNT_NAMEN}
    stapel = [verzeichnis]
    while stapel:
        aktuell = stapel.pop()
        with os.scandir(aktuell) as it:
            for eintrag in it:
                name = eintrag.name.casefold()
                if name in verboten_map:
                    sec_event("legal.mount.unsafe", "aborted", komponente="eingabe", reason="mapping")
                    raise MountUnsicher("mapping")
                if name in verboten_cli:
                    sec_event("legal.mount.unsafe", "aborted", komponente="eingabe", reason="cli_config")
                    raise MountUnsicher("cli_config")
                if eintrag.is_symlink():
                    sec_event("legal.mount.unsafe", "aborted", komponente="eingabe", reason="symlink")
                    raise MountUnsicher("symlink")
                if eintrag.is_dir(follow_symlinks=False):
                    stapel.append(Path(eintrag.path))


def _sammle_eintraege(root: Path) -> list[tuple[str, str]]:
    """Alle Einträge unter `root` rekursiv als (relativer Pfad, absoluter Pfad), sortiert.

    Folgt keinen Symlinks und steigt nicht in versteckte Ordner ab (sie werden als Eintrag
    gemeldet und von der Prüfung abgewiesen). Auf oberster Ebene bleiben `anweisung.md`,
    `ergebnis/` (nur als echtes Verzeichnis) und `HOLD` außen vor.
    """
    eintraege: list[tuple[str, str]] = []
    stapel: list[tuple[str, str]] = [(str(root), "")]
    besucht = 0
    while stapel:
        verzeichnis, praefix = stapel.pop()
        with os.scandir(verzeichnis) as it:
            for e in it:
                besucht += 1
                if besucht > MAX_EINTRAEGE:
                    raise _abweisen("size")
                if not praefix and e.name in AUSGENOMMEN_OBEN:
                    if e.name != "ergebnis" or e.is_dir(follow_symlinks=False):
                        continue
                rel = praefix + e.name
                eintraege.append((rel, e.path))
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    stapel.append((e.path, rel + "/"))
    eintraege.sort(key=lambda t: t[0])
    return eintraege


def pruefe_fallordner(pfad: Path, mapping_namen: tuple[str, ...]) -> Fallordner:
    """Fallordner gemäß TM-AC-1 prüfen; Abbruch vor jedem Modellaufruf bei Verstoß.

    Die Akte wird rekursiv aus dem Fallordner gelesen (siehe Moduldocstring).
    """
    try:
        root = Path(pfad).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _abweisen("path") from exc
    if not root.is_dir():
        raise _abweisen("path")
    anweisung = root / "anweisung.md"
    try:
        st_a = os.lstat(anweisung)
    except OSError as exc:
        raise _abweisen("missing_anweisung") from exc
    if stat.S_ISLNK(st_a.st_mode):
        raise _abweisen("symlink")
    if not stat.S_ISREG(st_a.st_mode):
        raise _abweisen("type")
    if st_a.st_nlink > 1:
        raise _abweisen("hardlink")
    _lies_text(anweisung, MAX_ANWEISUNG, None)

    eintraege = _sammle_eintraege(root)

    # Mapping-Gate zuerst und im ganzen Baum: eine Fehlablage wird als solche gemeldet (E-12).
    verboten_map = {n.casefold() for n in mapping_namen}
    if any(Path(rel).name.casefold() in verboten_map for rel, _ in eintraege):
        sec_event("legal.mount.unsafe", "aborted", komponente="eingabe", reason="mapping")
        raise MountUnsicher("mapping")

    dateien: list[Path] = []
    texte: list[str] = []
    gesamt = 0
    for index, (rel, pfad_abs) in enumerate(eintraege, start=1):
        name = Path(rel).name
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _abweisen("name", index) from exc
        st = os.lstat(pfad_abs)
        if name.startswith("."):
            raise _abweisen("hidden", index)
        if stat.S_ISLNK(st.st_mode):
            raise _abweisen("symlink", index)
        if stat.S_ISDIR(st.st_mode):
            continue
        if not stat.S_ISREG(st.st_mode):
            raise _abweisen("type", index)
        if Path(name).suffix.lower() not in ENDUNGEN:
            raise _abweisen("extension", index)
        if st.st_nlink > 1:
            raise _abweisen("hardlink", index)
        if st.st_size > MAX_DATEI:
            raise _abweisen("size", index)
        text = _lies_text(Path(pfad_abs), MAX_DATEI, index)
        gesamt += len(text.encode("utf-8"))   # tatsächlich gelesene Bytes, nicht lstat-Größe
        if gesamt > MAX_GESAMT:
            raise _abweisen("size", index)
        dateien.append(Path(pfad_abs))
        texte.append(text)
        if len(dateien) > MAX_DATEIEN:
            raise _abweisen("size", index)
    if not dateien:
        raise _abweisen("empty")
    return Fallordner(root=root, anweisung=anweisung, dateien=tuple(dateien), texte=tuple(texte))


# Schlüsselzeile, auch als Markdown: optionales Listenzeichen (`-`, `*`, `+`) und Fettdruck
# (`**Vergleich:** ja` oder `**Vergleich**: ja`). Gruppe 1 ist immer belegt (ggf. leer).
_SCHLUESSEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(\*\*|__|)(Ergebnis|Seite|Wortlimit|Personas|Vergleich)(?:\s*:\s*\1|\1\s*:)\s*(.*?)\s*$",
    re.IGNORECASE)


def lies_anweisung(pfad: Path) -> Anweisung:
    """`anweisung.md` lesen und Schlüsselzeilen (E-7) deterministisch auswerten.

    Ungültige oder doppelte Schlüsselzeilen führen zu `EingabeFehler` (Exit 2).
    """
    text = _lies_text(pfad, MAX_ANWEISUNG, None)
    werte: dict[str, str] = {}
    for zeile in text.splitlines():
        m = _SCHLUESSEL_RE.match(zeile)
        if not m:
            continue
        key = m.group(2).lower()
        if key in werte:
            raise _abweisen("instruction_duplicate")
        werte[key] = m.group(3)
    ergebnis = ERGEBNIS_DEFAULT
    if "ergebnis" in werte:
        ergebnis = werte["ergebnis"]
        if not (1 <= len(ergebnis) <= 80) or not re.fullmatch(r"[\w .,;:()/&+\-–]+", ergebnis):
            raise _abweisen("instruction_value")
    seite = None
    if "seite" in werte:
        norm = {"kläger": "Kläger", "klaeger": "Kläger", "beklagter": "Beklagter"}
        seite = norm.get(werte["seite"].lower())
        if seite is None:
            raise _abweisen("instruction_value")
    wortlimit = None
    if "wortlimit" in werte:
        if not re.fullmatch(r"\d{1,6}", werte["wortlimit"]):
            raise _abweisen("instruction_value")
        wortlimit = int(werte["wortlimit"])
        if not WORTLIMIT_MIN <= wortlimit <= WORTLIMIT_MAX:
            raise _abweisen("instruction_value")
    personas = False
    if "personas" in werte:
        v = werte["personas"].lower()
        if v not in ("an", "aus"):
            raise _abweisen("instruction_value")
        personas = v == "an"
    vergleich = False
    if "vergleich" in werte:
        v = werte["vergleich"].lower()
        if v not in ("ja", "nein"):
            raise _abweisen("instruction_value")
        vergleich = v == "ja"
    return Anweisung(text=text, ergebnis=ergebnis, seite=seite, wortlimit=wortlimit,
                     personas=personas, vergleich=vergleich, gesetzte_schluessel=tuple(sorted(werte)))


def kopiere_neutral(texte: tuple[str, ...], ziel: Path) -> dict[str, int]:
    """Akte unter neutralen Namen `dok_001.md` … nach `ziel` schreiben (TM-AC-2).

    Schreibt die vom Gate gelesenen und geprüften Inhalte (`Fallordner.texte`); die Akte wird
    nicht erneut gelesen, sodass ein Austausch nach der Prüfung nichts mehr einschleust.
    Rückgabe: `dok_nnn` → Index in der sortierten Dateiliste. Die Zuordnung zum
    Dateinamen steht nur im Host-Protokoll, nie im Log oder Mount.
    """
    zuordnung: dict[str, int] = {}
    for index, inhalt in enumerate(texte, start=1):
        dok = f"dok_{index:03d}"
        fd = os.open(ziel / f"{dok}.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(inhalt)
        zuordnung[dok] = index
    return zuordnung


# Heuristik für Anweisungsmuster in der Akte (TM-AC-8): nur Hinweis, kein Gate.
_INJEKTION_MUSTER: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions", re.compile(
        r"ignor\w*\s+(?:all\w*\s+)?(?:previous|prior|vorherig\w*|bisherig\w*|obig\w*)\s+"
        r"(?:instructions|anweisungen)", re.IGNORECASE)),
    ("rollenwechsel", re.compile(r"\b(?:du bist (?:jetzt|nun|ab sofort)|you are now|system prompt|systemprompt)\b",
                                 re.IGNORECASE)),
    ("steuerzeile", re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(?:Wortlimit|Personas|Vergleich|Seite|Ergebnis)"
                               r"(?:\*\*|__)?\s*:", re.IGNORECASE | re.MULTILINE)),
    ("vergleichsauftrag", re.compile(r"vergleichsspanne\s+(?:berechnen|nennen|angeben)", re.IGNORECASE)),
    ("freigabe", re.compile(r"\b(?:Timo hat (?:freigegeben|entschieden|zugestimmt)|approved by)\b", re.IGNORECASE)),
    ("schreibbefehl", re.compile(r"\b(?:schreibe|write|überschreibe|overwrite|lösche|delete)\b[^\n]{0,60}"
                                 r"\b(?:beratung\.md|entscheidungen\.md|register|protokoll|INJECTION)", re.IGNORECASE)),
    ("abruf", re.compile(r"\b(?:rufe|fetch|curl|wget|öffne)\b[^\n]{0,40}https?://", re.IGNORECASE)),
)


def injektions_heuristik(texte: dict[str, str]) -> list[tuple[str, str]]:
    """Anweisungsmuster je `dok_nnn` erkennen; Rückgabe (dok, Kategorie). Loggt nur die Anzahl."""
    treffer: list[tuple[str, str]] = []
    for dok in sorted(texte):
        for kategorie, muster in _INJEKTION_MUSTER:
            if muster.search(texte[dok]):
                treffer.append((dok, kategorie))
    if treffer:
        sec_event("legal.injection.suspected", "flagged", komponente="eingabe", count=len(treffer))
    return treffer
