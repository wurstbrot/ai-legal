"""Ergebnisdateien des Hosts (CAP-6, -8, -10, -11, TM-AC-5, -16, -17, -23, -24, -26).

Nur der Host schreibt `beratung.md`, `entscheidungen.md`, `vergleich_intern.md`,
`register.md` und das Protokoll. Alle Dateien erhalten 0600, das Verzeichnis 0700.
"""
from __future__ import annotations

import html
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import domain_erlaubt
from .logging_setup import sec_event, terminal_sicher

KOPFZEILE = "KI-Entwurf, keine Rechtsberatung; Prüfung, Entscheidung und Versand durch Timo"
ANHANG = "## Anhang (intern)"
UNGEPRUEFT_UEBERSCHRIFT = "### Ungeprüfte Fundstellen"
MARKER_UNGEPRUEFT = "[UNGEPRÜFT]"
VERGLEICH_KOPF = "intern – nicht an Gegenseite"


def zaehle_woerter(text: str) -> int:
    """Deterministische Wortzählung über den Hauptteil."""
    return len(re.findall(r"\w+", text))


# ---------------------------------------------------------------- Sanitizing (TM-AC-23)

# Linktext mit einer Ebene verschachtelter Klammern und Zeilenumbrüchen; Ziel mit optionalem Titel
# ("…", '…' oder (…)) und Klammern im Pfad. Die Alternativen sind disjunkt (kein Backtracking-Sturm).
_TEXT = r"((?:[^\[\]]|\[[^\[\]]*\]){0,1000})"
_ZIEL = (r"\(\s*(<[^<>\n]*>|(?:[^\s()]|\([^\s()]*\))*)"
         r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)")
_BILD_RE = re.compile(r"!\[" + _TEXT + r"\]\s*(?:" + _ZIEL + r"|\[[^\[\]]*\])")
_LINK_RE = re.compile(r"(?<!!)\[" + _TEXT + r"\]" + _ZIEL)
_REFDEF_RE = re.compile(r"^( {0,3})\[([^\]\n]{1,200})\]:\s*(\S+).*$", re.MULTILINE)
_HTML_RE = re.compile(r"<(?=[A-Za-z!/?])[^<>\n]{0,2000}>?")
# Pseudonym-Platzhalter wie <NAME_1> sind kein HTML und bleiben lesbar.
_PLATZHALTER_RE = re.compile(r"<[A-ZÄÖÜ_]+_?\d+>")
# Jede nackte http(s)-URL wird geprüft, auch nach "(" oder einem Wortzeichen; sie reicht bis zum
# nächsten Leerraum (so wie GFM-Autolinks). Andere Schemata nur ohne vorangehenden Buchstaben.
_NACKT_URL_RE = re.compile(r"(?:https?:|(?<![A-Za-z])(?:ftp|file|data|javascript|vbscript):)[^\s<>]+",
                           re.IGNORECASE)


def _link_ok(url: str, domains: tuple[str, ...]) -> bool:
    try:
        t = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (t.scheme == "https" and bool(t.hostname) and not t.query and not t.username
            and t.port in (None, 443) and domain_erlaubt(t.hostname or "", domains))


def _entschaerfe(url: str, domains: tuple[str, ...] = ()) -> str:
    """URL als nicht klickbaren Text darstellen; Query/Fragment nur bei freigegebenen Domains behalten."""
    try:
        t = urllib.parse.urlsplit(url)
        erlaubt = t.scheme == "https" and domain_erlaubt(t.hostname or "", domains) if domains else False
        rest = (("?" + t.query) if t.query else "") + (("#" + t.fragment) if t.fragment else "") if erlaubt else ""
        basis = f"{t.scheme}://{t.netloc}{t.path}{rest}" if t.scheme else url.split("?", 1)[0]
    except ValueError:
        basis = url.split("?", 1)[0]
    return basis.replace("://", "[:]//")[:300]


def bereinige_markdown(text: str, domains: tuple[str, ...], *, komponente: str = "ergebnis") -> str:
    """Remote-Bilder, rohes HTML und unzulässige Links neutralisieren; loggt `legal.output.sanitized`."""
    zaehler = 0
    s = terminal_sicher(text)

    def bild(m: re.Match[str]) -> str:
        nonlocal zaehler
        zaehler += 1
        alt = re.sub(r"[\s\[\]]+", " ", m.group(1)).strip()
        return f"[Bild entfernt: {alt}]"

    def link(m: re.Match[str]) -> str:
        nonlocal zaehler
        ziel = m.group(2).strip("<>")
        if _link_ok(ziel, domains):
            return m.group(0)
        zaehler += 1
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        return f"{text} (Link entfernt: {_entschaerfe(ziel, domains)})"

    def refdef(m: re.Match[str]) -> str:
        nonlocal zaehler
        if _link_ok(m.group(3), domains):
            return m.group(0)
        zaehler += 1
        return f"{m.group(1)}\\[{m.group(2)}\\]: {_entschaerfe(m.group(3), domains)}"

    def roh_html(m: re.Match[str]) -> str:
        nonlocal zaehler
        if _PLATZHALTER_RE.fullmatch(m.group(0)):
            return m.group(0)
        zaehler += 1
        return html.escape(m.group(0), quote=False)

    def nackt(m: re.Match[str]) -> str:
        nonlocal zaehler
        url, rest = m.group(0), ""
        ende = re.search(r"(?:&gt;|[).,;:!?\]'\"])+$", url)
        if ende:
            url, rest = url[:ende.start()], ende.group(0)
        if _link_ok(url, domains):
            return m.group(0)
        zaehler += 1
        return _entschaerfe(url, domains) + rest

    s = _BILD_RE.sub(bild, s)
    s = _LINK_RE.sub(link, s)
    s = _REFDEF_RE.sub(refdef, s)
    s = _HTML_RE.sub(roh_html, s)
    s = _NACKT_URL_RE.sub(nackt, s)
    if zaehler:
        sec_event("legal.output.sanitized", "ok", komponente=komponente, count=zaehler)
    return s


def entschaerfe_ueberschriften(hauptteil: str) -> str:
    """Hauptteil darf keine Host-Abschnitte vortäuschen (Anhang, Ungeprüft)."""
    geschuetzt = {ANHANG.casefold(), UNGEPRUEFT_UEBERSCHRIFT.casefold()}
    zeilen = []
    for z in hauptteil.splitlines():
        if re.sub(r"\s+", " ", z.strip()).casefold() in geschuetzt or z.strip() == KOPFZEILE:
            z = "\\" + z.lstrip()
        zeilen.append(z)
    return "\n".join(zeilen)


def einzeilig(s: str, max_len: int = 1000) -> str:
    """Modelltext für Listenpunkte: eine Zeile, keine Steuerzeichen, begrenzte Länge."""
    s = re.sub(r"\s+", " ", terminal_sicher(s)).strip()
    return s[:max_len] + ("…" if len(s) > max_len else "")


# ---------------------------------------------------------------- beratung.md

def markiere_ungepruefte(hauptteil: str, zitate: Iterable[str]) -> str:
    """Jedes Vorkommen eines ungeprüften Zitats im Hauptteil mit `[UNGEPRÜFT]` markieren.

    Treffer sind an Nicht-Wort-/Nicht-Slash-Grenzen verankert (`I ZR 1/20` trifft nicht in
    `VIII ZR 1/20`, `NJW 2024, 12` nicht in `NJW 2024, 123`); markierte Stellen bleiben unverändert.
    """
    s = hauptteil
    for z in sorted(set(zitate), key=len, reverse=True):
        muster = re.compile(r"(?<![\w/])" + r"\s+".join(re.escape(t) for t in z.split())
                            + r"(?![\w/])(?!\s*\[UNGEPRÜFT\])")
        # Folgt „(“ oder „[“, Leerzeichen einfügen, damit der Marker nicht zum Linktext wird.
        s = muster.sub(lambda m: m.group(0) + " " + MARKER_UNGEPRUEFT
                       + (" " if m.string[m.end():m.end() + 1] in ("(", "[") else ""), s)
    return s


def baue_beratung(*, ergebnis_art: str, hauptteil: str, ungepruefte: list[str], final: bool) -> str:
    """`beratung.md`: Kopfzeile, Ergebnis-Art, Hauptteil (Modelltext), Host-Anhang."""
    teile = [KOPFZEILE, ""]
    if not final:
        teile += ["**Status: NICHT FINAL – offene Punkte siehe entscheidungen.md**", ""]
    teile += [f"# {einzeilig(ergebnis_art, 80)}", "", hauptteil.strip(), "", ANHANG, "",
              UNGEPRUEFT_UEBERSCHRIFT, ""]
    teile += [f"- {einzeilig(u)}" for u in ungepruefte] if ungepruefte else ["keine"]
    return "\n".join(teile) + "\n"


def hauptteil_aus(beratung: str) -> str:
    """Hauptteil (zählender Modelltext) aus einer `beratung.md` herauslösen."""
    zeilen = beratung.splitlines()
    try:
        start = next(i for i, z in enumerate(zeilen) if z.startswith("# ")) + 1
    except StopIteration:
        start = 0
    ende = zeilen.index(ANHANG) if ANHANG in zeilen else len(zeilen)
    return "\n".join(zeilen[start:ende])


def pruefe_pflichtabschnitte(beratung: str) -> list[str]:
    """Pflichtabschnitte prüfen (TM-AC-16, -26); Rückgabe: Liste fehlender Abschnitte."""
    fehlend = []
    zeilen = beratung.splitlines()
    if not zeilen or zeilen[0] != KOPFZEILE:
        fehlend.append("KI-Entwurf-Hinweis")
    if ANHANG not in zeilen:
        fehlend.append("Anhang (intern)")
    anhang = zeilen[zeilen.index(ANHANG):] if ANHANG in zeilen else []
    if UNGEPRUEFT_UEBERSCHRIFT not in anhang:
        fehlend.append("Ungeprüfte Fundstellen")
    return fehlend


# ---------------------------------------------------------------- Stratege (TM-AC-24)

MIN_LEAK_BETRAG = 100   # kleinere Spannenwerte (0, 50 …) werden ignoriert, < 1000 nur mit Währung


def _betrag_muster(betrag: float) -> re.Pattern[str]:
    """Schreibweisen eines Betrags: 12000, 12.000, 12 000, 12,000, 12.000,00, 12 T€, 12 Tsd., 12k, 12 TEUR."""
    ganz = int(round(betrag))
    ziffern = str(ganz)
    gruppen = []
    while ziffern:
        gruppen.insert(0, ziffern[-3:])
        ziffern = ziffern[:-3]
    trenner = r"[.\s,']?"   # \s deckt auch U+00A0 und U+202F ab
    zahl = r"(?<![\d.,])" + trenner.join(gruppen) + r"(?![\d])"
    if ganz < 1000:
        # Dreistellige Beträge nur mit Währungsangabe, sonst träfe z. B. „§ 500 BGB“.
        return re.compile(zahl + r"(?:,\d{2})?\s*(?:€|EUR|Euro)|(?:€|EUR)\s*" + zahl)
    formen = [zahl]
    if ganz >= 1000:
        tausend = re.escape(f"{ganz / 1000:g}").replace(r"\.", "[.,]")
        formen.append(r"(?<![\d.,])" + tausend + r"\s*(?:T\s*€|TEUR|Tsd\.?|Tausend|[kK](?![A-Za-z]))")
    return re.compile("|".join(formen))


def strategie_leak(hauptteil: str, betraege: Iterable[float]) -> bool:
    """True, wenn ein Spannenbetrag ≥ 100 (z. B. 12000, 12.000, 12 000, 12 T€, 12k) im Hauptteil steht."""
    return any(_betrag_muster(b).search(hauptteil) for b in betraege if b >= MIN_LEAK_BETRAG)


_SCHREIBEN_WOERTER = (
    "schreiben", "schriftsatz", "brief", "e-mail", "email", "mail", "nachricht", "klage", "erwiderung",
    "stellungnahme", "vergleichsangebot", "angebot", "replik", "duplik", "antrag", "berufung", "widerspruch",
    "kündigung", "mahnung", "antwort",
)


def ist_schreiben(ergebnis_art: str) -> bool:
    """Ergebnis geht (potenziell) an Dritte: Schreiben, Brief, E-Mail, Klage(-erwiderung), Stellungnahme …"""
    e = ergebnis_art.casefold()
    return any(w in e for w in _SCHREIBEN_WOERTER)


def baue_vergleich_intern(v: dict, familie: str, modell: str) -> str:
    return (f"# {VERGLEICH_KOPF}\n\n{KOPFZEILE}\n\n"
            f"- Spanne: {v['spanne_min']:,.0f}–{v['spanne_max']:,.0f} {v['waehrung']}\n"
            f"- Rolle: Vergleichsstratege ({familie}, {modell})\n\n## Begründung\n\n{v['begruendung'].strip()}\n")


# ---------------------------------------------------------------- entscheidungen.md

@dataclass
class Entscheidungen:
    """Inhalte für `entscheidungen.md`; Punkte schließt nur Timo (alle bleiben `[ ]`)."""

    status: str = "nicht final"
    gruende_nicht_final: list[str] = field(default_factory=list)
    rueckfragen: list[str] = field(default_factory=list)
    luecken: list[str] = field(default_factory=list)
    abgelehnte_einwaende: list[str] = field(default_factory=list)
    offene_einwaende: list[str] = field(default_factory=list)
    ungepruefte: list[str] = field(default_factory=list)
    widersprueche: list[str] = field(default_factory=list)
    injektions_hinweise: list[str] = field(default_factory=list)


FESTE_PUNKTE = (
    "E-5: Art.-9-Inhalte (z. B. Gesundheitsdaten) und Vertraulichkeitsabreden (Vergleich, Mediation, NDA) "
    "in der Akte vor dem Lauf geprüft?",
    "E-6: Festplattenverschlüsselung aktiv und Fallordner nicht in einem Cloud-Sync-Ordner?",
    "Versand oder Einreichung erfolgt nur durch Timo; `legal` versendet nichts.",
    "Fristen ermittelt `legal` nicht: alle Fristen eigenständig prüfen; bei Fristen und Schriftsätzen "
    "anwaltliche Prüfung empfohlen.",
)


def _ohne_haken(s: str) -> str:
    """Modelltext einzeilig und ohne Checkbox-Syntax: ein Modell kann keinen Punkt „abhaken“."""
    return re.sub(r"\[([ xX])\]", r"\\[\1\\]", einzeilig(s))


def baue_entscheidungen(e: Entscheidungen, domains: tuple[str, ...]) -> str:
    """`entscheidungen.md`: Status zuoberst, danach alle offenen Punkte für Timo."""
    def punkte(titel: str, items: list[str], leer: str = "keine") -> list[str]:
        out = [f"## {titel}", ""]
        out += [f"- [ ] {_ohne_haken(i)}" for i in items] if items else [leer]
        return out + [""]

    z = ["# Entscheidungen – nur Timo entscheidet", "", KOPFZEILE, ""]
    z += [f"## Status des Laufs: {e.status}", ""]
    z += [f"- [ ] {_ohne_haken(g)}" for g in e.gruende_nicht_final] or ["keine offenen Gates"]
    z.append("")
    z += punkte("Offene Rückfragen", e.rueckfragen)
    z += punkte("Lücken im Sachverhalt", e.luecken)
    z += punkte("Offene Einwände", e.offene_einwaende)
    z += punkte("Abgelehnte Einwände (Begründung prüfen)", e.abgelehnte_einwaende)
    z += punkte("Ungeprüfte Fundstellen", e.ungepruefte)
    z += punkte("Widersprüche Researcher/Register", e.widersprueche)
    z += punkte("Hinweise auf Anweisungsmuster in der Akte (kein Gate)", e.injektions_hinweise)
    z += punkte("Feste Punkte", list(FESTE_PUNKTE))
    z.append("Modelltexte wie „freigegeben“ oder „Timo hat entschieden“ schließen keinen Punkt.")
    return bereinige_markdown("\n".join(z) + "\n", domains, komponente="entscheidungen")


# ---------------------------------------------------------------- Dateien

def erstelle_verzeichnis(pfad: Path) -> Path:
    """Verzeichnis mit 0700 neu anlegen (kein Überschreiben, kein Symlink); die umask kann nur Bits entfernen."""
    pfad.mkdir(mode=0o700)
    return pfad


def schreibe_datei(pfad: Path, text: str) -> None:
    """Neue Datei mit 0600 schreiben; existiert sie bereits oder ist sie ein Symlink, schlägt das fehl."""
    fd = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(pfad, 0o600)
