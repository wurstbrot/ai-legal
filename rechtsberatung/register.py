"""Rechtsprechungs-Register und Zitat-Gate (CAP-2, TM-AC-12, TM-AC-13).

Den Status eines Eintrags setzt ausschließlich der Host:
`geprüft` ⇔ https-URL auf der Recherche-Allowlist ∧ Bestätigung der anderen
Modellfamilie mit übereinstimmendem Gericht/Az./Datum ∧ Datum ≤ heute.
Eine abweichende Bestätigung ergibt `hält nicht`, alles andere `ungeprüft`.
"""
from __future__ import annotations

import datetime
import re
import urllib.parse
from dataclasses import dataclass, field

from .config import domain_erlaubt
from .logging_setup import sec_event

GEPRUEFT, UNGEPRUEFT, HAELT_NICHT = "geprüft", "ungeprüft", "hält nicht"

# Aktenzeichen: BSG (`B 1 KR 12/20 R`), allgemeine Form (`VIII ZR 123/24`, `1 BvR 123/20`,
# `5 AZR 1/19`, auch mit vierstelligem Jahr `/2020`) und BVerwG mit Punkt (`3 C 12.20`).
# Eine einzige Regex mit Alternativen: finditer liefert nicht überlappende Treffer, daher wird
# aus `B 1 KR 12/20 R` nicht zusätzlich `1 KR 12/20` extrahiert.
AZ_RE = re.compile(
    r"(?<![\w/.])(?:"
    r"B\s+\d{1,2}\s+[A-Z]{1,4}\s+\d{1,5}/\d{2}(?:\d{2})?\s+R"
    r"|(?:[IVX]{1,6}|\d{1,3})(?:-\d{1,3})?\s+[A-Z][A-Za-z]{0,5}(?:\s+\([A-Za-z]{1,6}\))?"
    r"\s+\d{1,5}/\d{2}(?:\d{2})?"
    r"|\d{1,2}\s+[A-Z]{1,3}\s+\d{1,5}\.\d{2}"
    r")(?![\w/])"
)
ECLI_RE = re.compile(r"\bECLI:[A-Z]{2}:[A-Za-z0-9.]{1,20}:\d{4}:[A-Za-z0-9.]{1,60}")
# Zeitschriften (Jahr, Seite) und amtliche Sammlungen (Band, Seite).
_ZEITSCHRIFTEN = ("NJW-RR", "NJW", "NZM", "BeckRS", "WuM", "ZMR", "NZA-RR", "NZA", "ZIP", "MDR", "FamRZ",
                  "VersR", "NVwZ", "NStZ", "BB", "DB")
_SAMMLUNGEN = ("BGHZ", "BVerfGE", "BAGE", "BSGE", "BVerwGE")
FUNDSTELLE_RE = re.compile(
    r"\b(?:(?:" + "|".join(re.escape(z) for z in _ZEITSCHRIFTEN) + r")\s+\d{4},\s*\d{1,7}"
    r"|(?:" + "|".join(_SAMMLUNGEN) + r")\s+\d{1,4},\s*\d{1,5})(?![\w/])"
)


def norm(s: str) -> str:
    """Whitespace normalisieren und Groß-/Kleinschreibung angleichen (für Abgleiche)."""
    return re.sub(r"\s+", " ", s).strip().casefold()


@dataclass
class Eintrag:
    """Registereintrag; `status` setzt nur der Host."""

    id: str
    gericht: str
    az: str
    datum: str
    url: str
    ecli: str = "-"
    zitate: list[str] = field(default_factory=list)
    status: str = UNGEPRUEFT
    grund: str = ""


@dataclass
class Register:
    """Register eines Laufs."""

    eintraege: list[Eintrag] = field(default_factory=list)

    def finde(self, zitat: str) -> tuple[Eintrag, bool] | None:
        """Eintrag zum Zitat; zweiter Wert True, wenn nur über die Researcher-Liste `zitate` gefunden.

        Die Bestätigung der anderen Familie deckt nur Gericht/Az./Datum ab; ein nur über
        `zitate` gefundenes Zitat (z. B. NJW-Fundstelle) gilt daher nie als geprüft.
        """
        z = norm(zitat)
        for e in self.eintraege:
            if z in (norm(e.az), norm(e.ecli)):
                return e, False
        for e in self.eintraege:
            if z in {norm(x) for x in e.zitate}:
                return e, True
        return None


def url_erlaubt(url: str, domains: tuple[str, ...]) -> bool:
    """https-URL ohne Benutzerdaten/Port-Tricks auf einer freigegebenen Domain."""
    try:
        teile = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if teile.scheme != "https" or not teile.hostname or teile.username or teile.password:
        return False
    if teile.port not in (None, 443):
        return False
    return domain_erlaubt(teile.hostname, domains)


def _datum_ok(s: str, heute: datetime.date) -> bool:
    try:
        return datetime.date.fromisoformat(s) <= heute
    except ValueError:
        return False


def baue_register(roh: dict, bestaetigungen: dict | None, *, domains: tuple[str, ...],
                  heute: datetime.date) -> Register:
    """Register aus Researcher-Ausgabe und Bestätigung der anderen Familie zusammenführen.

    Der Bestätiger erhält nur ID und URL (Blindbestätigung) und liest Gericht/Az./Datum selbst
    von der abgerufenen Seite; der Host vergleicht mit den Werten des Researchers.
    """
    best = {b["id"]: b for b in (bestaetigungen or {}).get("bestaetigungen", [])}
    reg = Register()
    for r in roh.get("eintraege", []):
        e = Eintrag(id=r["id"], gericht=r["gericht"], az=r["az"], datum=r["datum"], url=r["url"],
                    ecli=r.get("ecli", "-"), zitate=list(r.get("zitate", [])))
        b = best.get(e.id)
        if not url_erlaubt(e.url, domains):
            e.status, e.grund = UNGEPRUEFT, "Fundstelle nicht auf der Allowlist oder kein https"
        elif not _datum_ok(e.datum, heute):
            e.status, e.grund = UNGEPRUEFT, "Datum ungültig oder in der Zukunft"
        elif b is None or not b["abgerufen"]:
            e.status, e.grund = UNGEPRUEFT, "keine Bestätigung der anderen Modellfamilie"
        elif not (b["gericht"].strip() and b["az"].strip() and b["datum"].strip()):
            e.status, e.grund = UNGEPRUEFT, "Bestätigung unvollständig (Gericht, Az. oder Datum nicht ausgelesen)"
        elif (norm(b["gericht"]), norm(b["az"]), b["datum"].strip()) == (norm(e.gericht), norm(e.az), e.datum):
            e.status, e.grund = GEPRUEFT, "bestätigt durch andere Modellfamilie"
        else:
            e.status, e.grund = HAELT_NICHT, "Bestätigung weicht ab (Gericht, Az. oder Datum)"
        reg.eintraege.append(e)
    n = sum(1 for e in reg.eintraege if e.status != GEPRUEFT)
    if n:
        sec_event("legal.citation.unverified", "flagged", komponente="register", count=n)
    return reg


def register_markdown(reg: Register) -> str:
    """Register als Markdown-Tabelle (vom Host geschrieben)."""
    zeilen = ["# Register", "", "| ID | Gericht | Az. | Datum | Fundstelle | Zitate | Status | Grund |",
              "|---|---|---|---|---|---|---|---|"]
    for e in reg.eintraege:
        zellen = [e.id, e.gericht, e.az, e.datum, e.url, "; ".join(e.zitate) or "-", e.status, e.grund]
        zeilen.append("| " + " | ".join(_zelle(z) for z in zellen) + " |")
    if not reg.eintraege:
        zeilen.append("| – | – | – | – | – | – | – | Register leer |")
    return "\n".join(zeilen) + "\n"


def _zelle(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def extrahiere_zitate(text: str) -> list[str]:
    """Az., ECLI und Fundstellen (NJW, NJW-RR, NZM, BeckRS) deterministisch extrahieren."""
    gefunden: list[str] = []
    for rx in (ECLI_RE, AZ_RE, FUNDSTELLE_RE):
        for m in rx.finditer(text):
            z = re.sub(r"\s+", " ", m.group(0)).strip()
            if z not in gefunden:
                gefunden.append(z)
    return gefunden


@dataclass
class ZitatPruefung:
    """Ergebnis des Zitat-Gates."""

    unregistriert: list[str] = field(default_factory=list)
    ungeprueft: list[tuple[str, Eintrag]] = field(default_factory=list)
    haelt_nicht: list[tuple[str, Eintrag]] = field(default_factory=list)
    nur_zitat: set[str] = field(default_factory=set)

    @property
    def widersprueche(self) -> list[str]:
        """Widersprüche der Rolle „Researcher/Register“ für das Protokoll."""
        out = [f"Zitat ohne Registereintrag: {z}" for z in self.unregistriert]
        out += [f"Zitat hält nicht (Bestätigung weicht ab): {z}" for z, _ in self.haelt_nicht]
        return out


def zitat_gate(text: str, reg: Register) -> ZitatPruefung:
    """Jedes Zitat braucht einen Registereintrag (TM-AC-12); Status bestimmt die Markierung."""
    pr = ZitatPruefung()
    for z in extrahiere_zitate(text):
        treffer = reg.finde(z)
        if treffer is None:
            pr.unregistriert.append(z)
            continue
        e, nur_zitat = treffer
        if e.status == HAELT_NICHT:
            pr.haelt_nicht.append((z, e))
        elif nur_zitat or e.status != GEPRUEFT:
            pr.ungeprueft.append((z, e))
            if nur_zitat:
                pr.nur_zitat.add(z)
    if pr.unregistriert:
        sec_event("legal.citation.unregistered", "rejected", komponente="register", count=len(pr.unregistriert))
    return pr
