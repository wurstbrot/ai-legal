"""Deterministischer Conductor (CAP-1–CAP-11, TM-AC-3, -9, -10, -12–-20, -24).

Der Conductor ist Host-Python ohne LLM. Er führt die Rollen gemäß
`ablauf-diagramm.md` in Containern aus (Runner injizierbar), prüft jede
Ausgabe auf dem Host, erzwingt die Gates und schreibt als einziger Ergebnis,
Register, Protokoll und `entscheidungen.md`. Jede Rollen-Fehlfunktion beendet
die Pipeline fail-closed; Protokoll und `entscheidungen.md` entstehen trotzdem.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import prompts
from .config import FAMILIEN, Konfig
from .container import Aufruf, RunErgebnis, netz_fuer_rolle
from .eingabe import Anweisung, Fallordner, MountUnsicher, injektions_heuristik, kopiere_neutral, pruefe_mapping
from .ergebnis import (
    Entscheidungen, FristEintrag, baue_beratung, baue_entscheidungen, baue_vergleich_intern,
    bereinige_markdown, einzeilig, entschaerfe_ueberschriften, ist_schreiben, markiere_ungepruefte,
    pruefe_pflichtabschnitte, schreibe_datei, strategie_leak, zaehle_woerter,
)
from .logging_setup import progress, sec_event
from .register import Register, baue_register, register_markdown, zitat_gate
from .rueckkanal import (
    RolleFehlgeschlagen, lies_json, schema_bestaetigung, schema_coldread, schema_entwurf, schema_fristen,
    schema_groesse, schema_persona, schema_rechtsfragen, schema_register, schema_review,
    schema_sachverhalt, schema_ueberarbeitung, schema_vergleich,
)

Runner = Callable[[Aufruf], RunErgebnis]

EXIT_FINAL, EXIT_FEHLER, EXIT_EINGABE, EXIT_NICHT_FINAL, EXIT_STOPP = 0, 1, 2, 3, 4

_EINGABE_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}\.(?:md|json)$")
_DOK_RE = re.compile(r"^dok_\d{3}$")
_NORM_RE = re.compile(r"(?:§§?|Art\.)\s*\d+")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b")
_TELEFON_RE = re.compile(r"(?<![\w/.])(?:\+\d{1,3}[\s/\-]?|0)[1-9]\d{1,4}[\s/\-]?\d{3,}(?:[\s\-]?\d{2,})*(?![\w/])")
MAX_RECHTSFRAGEN = 8 * 1024


class Stopp(RuntimeError):
    """Hold-Datei, Budget oder Laufzeit erreicht (Exit 4)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnabhaengigkeitVerletzt(RuntimeError):
    """Prüfende Rolle hätte dieselbe Modellfamilie wie der Autor (TM-AC-14)."""


@dataclass
class Einwand:
    """Einwand oder Hinweis gegen eine Fassung; schließen kann ihn nur eine begründete Antwort."""

    id: str
    rolle: str
    familie: str
    text: str
    runde: int
    blockierend: bool = True
    status: str | None = None
    begruendung: str = ""

    @property
    def offen(self) -> bool:
        return self.blockierend and (self.status is None or not self.begruendung.strip())


@dataclass
class Entwurf:
    """Fassung eines Mandanten-Anwalts (Autor = Modellfamilie)."""

    familie: str
    hauptteil: str
    einwaende: list[Einwand] = field(default_factory=list)
    review_note: float | None = None


def andere(familie: str) -> str:
    """Die jeweils andere Modellfamilie."""
    return "codex" if familie == "claude" else "claude"


def _sha256(daten: bytes) -> str:
    return hashlib.sha256(daten).hexdigest()


def _hashes(verzeichnis: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(verzeichnis.rglob("*")):
        if p.is_symlink():
            out[str(p.relative_to(verzeichnis))] = "symlink"
        elif p.is_file():
            out[str(p.relative_to(verzeichnis))] = _sha256(p.read_bytes())
    return out


def _jetzt() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Protokoll:
    """Append-only Protokoll (`protokoll.jsonl` und `protokoll.md`), nur vom Host geschrieben."""

    def __init__(self, ziel: Path, lauf_id: str, domains: tuple[str, ...]) -> None:
        self.lauf_id = lauf_id
        self.domains = domains
        self._lock = threading.Lock()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        self._jsonl = os.fdopen(os.open(ziel / "protokoll.jsonl", flags, 0o600), "a", encoding="utf-8")
        self._md = os.fdopen(os.open(ziel / "protokoll.md", flags, 0o600), "a", encoding="utf-8")

    def eintrag(self, **felder: Any) -> None:
        with self._lock:
            self._jsonl.write(json.dumps({"lauf_id": self.lauf_id, **felder}, ensure_ascii=False) + "\n")
            self._jsonl.flush()

    def md(self, text: str) -> None:
        sauber = bereinige_markdown(text, self.domains, komponente="protokoll")
        with self._lock:
            self._md.write(sauber.rstrip("\n") + "\n\n")
            self._md.flush()

    def schliessen(self) -> None:
        for f in (self._jsonl, self._md):
            try:
                f.close()
            except OSError:
                pass


class Conductor:
    """Führt einen Lauf aus; `lauf()` liefert den Exit-Code."""

    def __init__(self, *, konfig: Konfig, fall: Fallordner, anweisung: Anweisung, ergebnis_dir: Path,
                 arbeits_dir: Path, runner: Runner, lauf_id: str, heute: datetime.date | None = None,
                 uhr: Callable[[], float] = time.monotonic, image_id: str = "") -> None:
        self.k = konfig
        self.image_id = image_id or "nicht aufgelöst (injizierter Runner)"
        self.fall = fall
        self.a = anweisung
        self.ergebnis = ergebnis_dir
        self.arbeit = arbeits_dir
        self.runner = runner
        self.lauf_id = lauf_id
        self.heute = heute or datetime.date.today()
        self.uhr = uhr
        self.start = uhr()
        self.hold = fall.root / "HOLD"
        self.aufrufe = 0
        self._lock = threading.Lock()
        self._einwand_nr = 0
        self.rueckfragen: list[str] = []
        self.doks: dict[str, str] = {}
        self.eingaben_basis: dict[str, str] = {}
        self.register = Register()
        self.e = Entscheidungen()
        self.protokoll: Protokoll | None = None

    # ------------------------------------------------------------ Steuerung

    def _pruefe_stopp(self) -> None:
        """Hold-Datei, Aufrufbudget und Laufzeit prüfen (vor jeder Runde und jedem Container)."""
        if os.path.lexists(self.hold):
            sec_event("legal.hold.stop", "aborted", aufrufe=self.aufrufe)
            raise Stopp("hold")
        if self.aufrufe >= self.k.budget.max_aufrufe:
            sec_event("legal.budget.exceeded", "aborted", reason="aufrufe", aufrufe=self.aufrufe)
            raise Stopp("budget")
        if self.uhr() - self.start > self.k.budget.max_laufzeit_s:
            sec_event("legal.budget.exceeded", "aborted", reason="laufzeit", aufrufe=self.aufrufe)
            raise Stopp("laufzeit")

    def _naechste_einwand_id(self) -> str:
        with self._lock:
            self._einwand_nr += 1
            return f"E{self._einwand_nr:03d}"

    def _md(self, text: str) -> None:
        if self.protokoll:
            self.protokoll.md(text)

    def _parallel(self, fns: list[Callable[[], Any]]) -> list[Any]:
        """Unabhängige Rollen parallel ausführen; der erste Fehler wird nach Abschluss aller weitergereicht."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(fns))) as ex:
            futures = [ex.submit(f) for f in fns]
            ergebnisse: list[Any] = []
            fehler: BaseException | None = None
            for fu in futures:
                try:
                    ergebnisse.append(fu.result())
                except BaseException as exc:  # noqa: BLE001 - wird unten weitergereicht
                    if isinstance(exc, KeyboardInterrupt) or fehler is None:
                        fehler = exc   # Signal hat Vorrang vor Rollenfehlern (Exit 4 statt 3)
                    ergebnisse.append(None)
        if fehler is not None:
            raise fehler
        return ergebnisse

    def _rolle(self, rolle: str, familie: str, eingaben: dict[str, str], prompt: str, ausgabe: str,
               schema: Callable[[Any], Any], *, runde: int = 0, autor: str | None = None) -> Any:
        """Eine Rolle im Container ausführen und ihre Ausgabe auf dem Host prüfen."""
        if autor is not None and familie == autor:
            sec_event("legal.independence.violated", "aborted", rolle=rolle, familie=familie)
            raise UnabhaengigkeitVerletzt(rolle)
        with self._lock:
            self._pruefe_stopp()
            self.aufrufe += 1
            nr = self.aufrufe
        basis = self.arbeit / f"{nr:03d}_{rolle}_{familie}"
        in_dir, out_dir, home_dir = basis / "in", basis / "out", basis / "home"
        for d in (basis, in_dir, out_dir, home_dir):
            d.mkdir(mode=0o700)
        for name, inhalt in eingaben.items():
            if not _EINGABE_NAME_RE.fullmatch(name):
                raise ValueError("unzulässiger Eingabename")
            schreibe_datei(in_dir / name, inhalt)
        pruefe_mapping(in_dir, self.k.mapping_namen)
        vorher = _hashes(in_dir)
        modell = self.k.modell(rolle, familie)
        aufruf = Aufruf(
            rolle=rolle, familie=familie, netz=netz_fuer_rolle(rolle), in_dir=in_dir, out_dir=out_dir,
            home_dir=home_dir, prompt=prompt, name=f"legal-{self.lauf_id}-{rolle.replace('_', '-')}-{nr}",
            timeout=self.k.budget.timeout_s, modell=self.k.modell_argument(rolle, familie), lauf_id=self.lauf_id,
        )
        start, t0 = _jetzt(), self.uhr()
        sec_event("legal.container.start", "ok", komponente="container", rolle=rolle, familie=familie, modell=modell)
        res = self.runner(aufruf)
        dauer = round(self.uhr() - t0, 1)
        sec_event("legal.container.done", "ok" if res.rc == 0 else "failed", komponente="container",
                  rolle=rolle, familie=familie, modell=modell, rc=res.rc, dauer_s=dauer)
        ausgabe_hash = ""
        try:
            if _hashes(in_dir) != vorher:
                sec_event("legal.output.write_denied", "aborted", rolle=rolle, familie=familie)
                raise RolleFehlgeschlagen("write_denied", rolle)
            if res.timed_out:
                raise RolleFehlgeschlagen("timeout", rolle)
            if res.rc != 0:
                raise RolleFehlgeschlagen("rc", rolle)
            daten = lies_json(out_dir, ausgabe, schema, rolle=rolle)
            ausgabe_hash = _sha256((out_dir / ausgabe).read_bytes())
        finally:
            if self.protokoll:
                self.protokoll.eintrag(
                    typ="container", nr=nr, runde=runde, rolle=rolle, familie=familie, modell=modell,
                    image_id=self.image_id,
                    start=start, ende=_jetzt(), rc=res.rc, timed_out=res.timed_out,
                    eingaben_sha256=vorher, ausgabe_sha256=ausgabe_hash,
                )
        for r in daten.get("rueckfragen", []) if isinstance(daten, dict) else []:
            self.rueckfragen.append(f"{rolle} ({familie}, {modell}): {r}")
        return daten

    # ------------------------------------------------------------ Lauf

    def lauf(self) -> int:
        """Pipeline ausführen; Protokoll und `entscheidungen.md` werden immer geschrieben."""
        code = EXIT_NICHT_FINAL
        self.protokoll = Protokoll(self.ergebnis, self.lauf_id, self.k.research_domains)
        try:
            code = self._pipeline()
        except Stopp as s:
            code = EXIT_STOPP
            self.e.gruende_nicht_final.append(f"Lauf gestoppt ({s.reason}) – keine weitere Rolle gestartet")
        except RolleFehlgeschlagen as r:
            code = EXIT_NICHT_FINAL
            self.e.gruende_nicht_final.append(f"Rolle {r.rolle or '?'} fehlgeschlagen ({r.reason}) – Kette beendet")
        except UnabhaengigkeitVerletzt as u:
            code = EXIT_NICHT_FINAL
            self.e.gruende_nicht_final.append(f"Unabhängigkeit verletzt bei Rolle {u} – Kette beendet")
        except MountUnsicher as m:
            code = EXIT_EINGABE
            self.e.gruende_nicht_final.append(f"Mount-Gate: {m.reason}")
        except KeyboardInterrupt:
            code = EXIT_STOPP
            self.e.gruende_nicht_final.append("Lauf durch Signal abgebrochen – Container des Laufs beendet")
        except Exception:  # noqa: BLE001 - fail-closed, Details nicht ins Ergebnis oder Log
            code = EXIT_FEHLER
            sec_event("legal.run.error", "aborted", reason="unexpected")
            self.e.gruende_nicht_final.append("Unerwarteter Fehler – Lauf abgebrochen")
        finally:
            self.e.status = "final" if code == EXIT_FINAL else "nicht final"
            self.e.rueckfragen = list(dict.fromkeys(self.rueckfragen))
            try:
                self._md(f"## Abschluss\n\n- Status: {self.e.status} (Exit {code})\n- Modellaufrufe: {self.aufrufe}")
                schreibe_datei(self.ergebnis / "entscheidungen.md",
                               baue_entscheidungen(self.e, self.k.research_domains))
            finally:
                self.protokoll.schliessen()
            if code == EXIT_FINAL:
                sec_event("legal.run.final", "ok", aufrufe=self.aufrufe)
            else:
                sec_event("legal.run.not_final", "aborted" if code != EXIT_NICHT_FINAL else "rejected",
                          exit=code, aufrufe=self.aufrufe)
        return code

    def _pipeline(self) -> int:
        a = self.a
        self._md(
            f"# Protokoll Lauf {self.lauf_id}\n\n- Datum: {self.heute.isoformat()}\n- Ergebnis-Art: {a.ergebnis}\n"
            f"- Mandantenseite: {a.seite or 'nicht angegeben'}\n- Wortlimit: {a.wortlimit or 'keins'}\n"
            f"- Personas: {'an' if a.personas else 'aus'}\n- Vergleich: {'ja' if a.vergleich else 'nein'}\n"
            f"- Gesetzte Schlüsselzeilen (nur aus anweisung.md): {', '.join(a.gesetzte_schluessel) or 'keine'}\n"
            f"- Gewichte: " + ", ".join(f"{r} {w}" for r, w in self.k.gewichte.items())
            + f"\n- Image: {self.k.image} → {self.image_id}"
        )
        if self.protokoll:
            self.protokoll.eintrag(typ="lauf", image=self.k.image, image_id=self.image_id,
                                   ergebnis_art=a.ergebnis, start=_jetzt())
        if a.seite is None:
            self.rueckfragen.append("Conductor: Mandantenseite (Seite: Kläger|Beklagter) fehlt in anweisung.md")

        # Akte unter neutralen Namen (TM-AC-2); Zuordnung nur im Host-Protokoll.
        akte = self.arbeit / "akte"
        akte.mkdir(mode=0o700)
        zuordnung = kopiere_neutral(self.fall.texte, akte)
        self.doks = {dok: (akte / f"{dok}.md").read_text(encoding="utf-8") for dok in zuordnung}
        self._md("## Zuordnung dok ↔ Datei (nur Host)\n\n" + "\n".join(
            f"- {dok} ← {einzeilig(str(self.fall.dateien[i - 1].relative_to(self.fall.root)), 200)}"
            for dok, i in zuordnung.items()))
        for dok, kategorie in injektions_heuristik(self.doks):
            self.e.injektions_hinweise.append(f"{dok}: Anweisungsmuster „{kategorie}“ (wird als Daten behandelt)")
        akte_eingaben = {f"{dok}.md": text for dok, text in self.doks.items()}

        # 1. Sachverhalt ×2 mit Abgleich und Quellen-Gate.
        sv_eingaben = {**akte_eingaben, "anweisung.md": a.text}
        sv = dict(zip(FAMILIEN, self._parallel([
            (lambda f=f: self._rolle("sachverhalt", f, sv_eingaben, prompts.sachverhalt(a), "sachverhalt.json",
                                     schema_sachverhalt)) for f in FAMILIEN])))
        sachverhalt_md = self.abgleich_sachverhalt(sv)
        self._md(sachverhalt_md.replace("# Sachverhalt", "## Sachverhalt", 1))

        # 2. Rechtsfragen (Claude) und Researcher-Minimierung (TM-AC-9).
        rf = self._rolle("rechtsfragen", "claude", {"sachverhalt.md": sachverhalt_md, "anweisung.md": a.text},
                         prompts.rechtsfragen(a), "rechtsfragen.json", schema_rechtsfragen)
        rechtsfragen_md = "# Rechtsfragen\n\n" + "\n".join(f"- {einzeilig(q)}" for q in rf["fragen"]) + \
            "\n\n# Normen\n\n" + "\n".join(f"- {einzeilig(n)}" for n in rf["normen"]) + "\n"
        self._md("## Rechtsfragen (rechtsfragen, claude, " + self.k.modell("rechtsfragen", "claude") + ")\n\n"
                 + rechtsfragen_md.replace("# ", "### "))
        roh_register: dict = {"eintraege": []}
        bestaetigung: dict | None = None
        verstoss = self.pruefe_rechtsfragen(rechtsfragen_md)
        if verstoss:
            sec_event("legal.researcher.input_rejected", "rejected", reason=verstoss)
            self.e.widersprueche.append(f"Researcher nicht gestartet: Rechtsfragen verletzen die Minimierung ({verstoss})")
        else:
            # 3. Researcher (Claude, Recherche-Netz) und 4. Register-Bestätiger (Codex).
            roh_register = self._rolle("researcher", "claude", {"rechtsfragen.md": rechtsfragen_md},
                                       prompts.researcher(self.k.research_domains), "register.json", schema_register)
            if roh_register["eintraege"]:
                # Blindbestätigung: nur ID und URL; Gericht/Az./Datum liest der Bestätiger selbst aus.
                nur_eintraege = [{k: e[k] for k in ("id", "url")} for e in roh_register["eintraege"]]
                bestaetigung = self._rolle(
                    "registercheck", "codex",
                    {"register_eintraege.json": json.dumps(nur_eintraege, ensure_ascii=False, indent=1)},
                    prompts.registercheck(), "bestaetigung.json", schema_bestaetigung, autor="claude")
        self.register = baue_register(roh_register, bestaetigung, heute=self.heute)
        register_md = bereinige_markdown(register_markdown(self.register), self.k.research_domains,
                                         komponente="register")
        schreibe_datei(self.ergebnis / "register.md", register_md)
        self._md("## Register (Status vom Host)\n\n" + register_md.replace("# Register\n", ""))

        basis = {**akte_eingaben, "anweisung.md": a.text, "sachverhalt.md": sachverhalt_md, "register.md": register_md}

        # 5. Mandanten-Anwalt ×2, unabhängig in getrennten Containern.
        entwuerfe: dict[str, Entwurf] = {}
        for f, d in zip(FAMILIEN, self._parallel([
                (lambda f=f: self._rolle("anwalt", f, basis, prompts.anwalt(a), "entwurf.json", schema_entwurf))
                for f in FAMILIEN])):
            entwuerfe[f] = Entwurf(familie=f, hauptteil=d["hauptteil"])
            self._md(f"## Entwurf Mandanten-Anwalt ({f}, {self.k.modell('anwalt', f)})\n\n{d['hauptteil']}")

        # 6. Cross-Review: Review durch die andere Familie, Überarbeitung durch den Autor.
        for r in range(1, self.k.budget.review_runden + 1):
            with self._lock:
                self._pruefe_stopp()
            reviews = self._parallel([
                (lambda f=f: self._rolle("review", andere(f), {**basis, "entwurf.md": entwuerfe[f].hauptteil},
                                         prompts.review(a), "review.json", schema_review, runde=r, autor=f))
                for f in FAMILIEN])
            for f, rv in zip(FAMILIEN, reviews):
                entwuerfe[f].review_note = rv["note"]
                self._uebernehme_einwaende(entwuerfe[f], "review", andere(f), rv, r)
            self._parallel([(lambda f=f: self._ueberarbeite(entwuerfe[f], basis, [], r)) for f in FAMILIEN])

        # 7. Benotung: Richter und Gegenanwalt der jeweils anderen Familie bewerten beide Entwürfe.
        aufgaben = [(f, rolle) for f in FAMILIEN for rolle in ("richter", "gegenanwalt")]
        bewertungen = dict(zip(aufgaben, self._parallel([
            (lambda f=f, rolle=rolle: self._rolle(
                rolle, andere(f), {**basis, "entwurf.md": entwuerfe[f].hauptteil},
                prompts.richter(a) if rolle == "richter" else prompts.gegenanwalt(a), "bewertung.json",
                schema_review, autor=f))
            for f, rolle in aufgaben])))
        gesamt = {f: self.gesamtnote(entwuerfe[f].review_note, bewertungen[(f, "richter")]["note"],
                                     bewertungen[(f, "gegenanwalt")]["note"]) for f in FAMILIEN}
        gewinner_f = max(FAMILIEN, key=lambda f: (gesamt[f], f == "claude"))
        w = self.k.gewichte
        tabelle = ["## Benotung", "", "| Entwurf | Mandanten-Anwalt (Review) | Richter | Gegenanwalt | Gesamtnote |",
                   "|---|---|---|---|---|"]
        for f in FAMILIEN:
            tabelle.append(
                f"| {f} | {entwuerfe[f].review_note:.2f} ({andere(f)}) | {bewertungen[(f, 'richter')]['note']:.2f} "
                f"({andere(f)}) | {bewertungen[(f, 'gegenanwalt')]['note']:.2f} ({andere(f)}) | {gesamt[f]:.3f} |")
        tabelle += ["", f"Gewichte: Mandanten-Anwalt {w['anwalt']}, Richter {w['richter']}, "
                        f"Gegenanwalt {w['gegenanwalt']}; Gesamtnote = Σ wᵢ·noteᵢ / Σ wᵢ.",
                    f"Gewählte Fassung: **{gewinner_f}** (Gleichstand → Claude)."]
        self._md("\n".join(tabelle))
        sieger = entwuerfe[gewinner_f]
        pruefer = andere(gewinner_f)
        for rolle in ("richter", "gegenanwalt"):
            self._uebernehme_einwaende(sieger, rolle, pruefer, bewertungen[(gewinner_f, rolle)], 0)

        # 8. Fristen ×2 (beide Familien, unabhängig).
        fr_eingaben = {**akte_eingaben, "anweisung.md": a.text, "sachverhalt.md": sachverhalt_md}
        fr = dict(zip(FAMILIEN, self._parallel([
            (lambda f=f: self._rolle("fristen", f, fr_eingaben, prompts.fristen(a, self.heute.isoformat()),
                                     "fristen.json", schema_fristen)) for f in FAMILIEN])))
        fristen, risiken = self.abgleich_fristen(fr)

        # 9. Stratege nur mit Auftrag aus anweisung.md (TM-AC-18, -24).
        vergleich = None
        if a.vergleich:
            bew_md = "\n\n".join(
                f"## {rolle} ({pruefer})\n\nNote: {bewertungen[(gewinner_f, rolle)]['note']}\n\n"
                f"{bewertungen[(gewinner_f, rolle)]['begruendung']}" for rolle in ("richter", "gegenanwalt"))
            vergleich = self._rolle("stratege", pruefer,
                                    {"entwurf.md": sieger.hauptteil, "sachverhalt.md": sachverhalt_md,
                                     "bewertungen.md": bew_md, "anweisung.md": a.text},
                                    prompts.stratege(a), "vergleich.json", schema_vergleich, autor=gewinner_f)
            self._md(f"## Vergleichsstratege ({pruefer}, {self.k.modell('stratege', pruefer)})\n\n"
                     "Spanne nur in vergleich_intern.md (intern – nicht an Gegenseite).")

        # 10. Schleife: Personas → Überarbeitung → Größen-Prüfer → Cold-Read.
        hinweise: list[Einwand] = []
        for runde in range(1, self.k.budget.max_runden + 1):
            with self._lock:
                self._pruefe_stopp()
            if a.personas:
                for art, d in zip(("persona_klaeger", "persona_beklagter"), self._parallel([
                        (lambda art=art: self._rolle(
                            art, pruefer, {"entwurf.md": sieger.hauptteil, "sachverhalt.md": sachverhalt_md,
                                           "anweisung.md": a.text},
                            prompts.persona(art, a), "persona.json", schema_persona, runde=runde, autor=gewinner_f))
                        for art in ("persona_klaeger", "persona_beklagter")])):
                    self._md(f"### Runde {runde} – {art} ({pruefer}, {self.k.modell(art, pruefer)})\n\n"
                             + "\n".join(f"- {einzeilig(h)}" for h in d["hinweise"]))
                    for h in d["hinweise"]:
                        sieger.einwaende.append(Einwand(self._naechste_einwand_id(), art, pruefer, h, runde))
            woerter = zaehle_woerter(sieger.hauptteil)
            if a.wortlimit and woerter > a.wortlimit:
                hinweise.append(Einwand(self._naechste_einwand_id(), "conductor", "host",
                                        f"Hauptteil hat {woerter} Wörter, Wortlimit {a.wortlimit}.", runde,
                                        blockierend=False))
            if any(e.offen for e in sieger.einwaende) or hinweise:
                self._ueberarbeite(sieger, basis, hinweise, runde)
            hinweise = []
            vorher = zaehle_woerter(sieger.hauptteil)
            gr = self._rolle("groesse", pruefer, {"entwurf.md": sieger.hauptteil}, prompts.groesse(a),
                             "groesse.json", schema_groesse, runde=runde, autor=gewinner_f)
            zeilen = [f"### Runde {runde} – Größen-Prüfer ({pruefer}, {self.k.modell('groesse', pruefer)})", "",
                      f"Host-Wortzählung Hauptteil: {vorher}" + (f" (Limit {a.wortlimit})" if a.wortlimit else ""),
                      "", "| Abschnitt | Wörter vorher | Wörter nachher | Vorschlag |", "|---|---|---|---|"]
            for v in gr["vorschlaege"]:
                zeilen.append(f"| {einzeilig(v['abschnitt'], 120)} | {v['woerter_vorher']} | {v['woerter_nachher']} "
                              f"| {einzeilig(v['vorschlag'], 400)} |")
                hinweise.append(Einwand(self._naechste_einwand_id(), "groesse", pruefer,
                                        f"Kürzung {v['abschnitt']}: {v['woerter_vorher']}→{v['woerter_nachher']} "
                                        f"Wörter. {v['vorschlag']}", runde, blockierend=False))
            if not gr["vorschlaege"]:
                zeilen.append("| – | – | – | keine Kürzungsvorschläge |")
            self._md("\n".join(zeilen))
            cr = self._rolle("coldread", pruefer,
                             {**akte_eingaben, "endfassung.md": sieger.hauptteil, "register.md": register_md},
                             prompts.coldread(), "befunde.json", schema_coldread, runde=runde, autor=gewinner_f)
            self._md(f"### Runde {runde} – Cold-Read Zitat-/Fakten-Prüfer ({pruefer}, "
                     f"{self.k.modell('coldread', pruefer)})\n\n"
                     + ("\n".join(f"- {einzeilig(b['aussage'])} – {einzeilig(b['grund'])}" for b in cr["befunde"])
                        or "keine Befunde"))
            for b in cr["befunde"]:
                sieger.einwaende.append(Einwand(self._naechste_einwand_id(), "coldread", pruefer,
                                                f"{b['aussage']} – {b['grund']}", runde))
            limit_ok = not a.wortlimit or vorher <= a.wortlimit
            if limit_ok and not cr["befunde"] and not any(e.offen for e in sieger.einwaende):
                break

        return self._abschluss(sieger, fristen, risiken, vergleich, pruefer)

    # ------------------------------------------------------------ Hilfsschritte

    def _uebernehme_einwaende(self, entwurf: Entwurf, rolle: str, familie: str, daten: dict, runde: int) -> None:
        modell = self.k.modell(rolle, familie)
        zeilen = [f"### {'Runde ' + str(runde) + ' – ' if runde else ''}{rolle} zu Entwurf {entwurf.familie} "
                  f"({familie}, {modell})", "", f"Note: {daten['note']:.2f}", "", einzeilig(daten["begruendung"], 2000)]
        for e in daten["einwaende"]:
            eid = self._naechste_einwand_id()
            entwurf.einwaende.append(Einwand(eid, rolle, familie, e["text"], runde))
            zeilen.append(f"- Einwand {eid}: {einzeilig(e['text'])}")
        self._md("\n".join(zeilen))

    def _ueberarbeite(self, entwurf: Entwurf, basis: dict[str, str], hinweise: list[Einwand], runde: int) -> None:
        offen = [e for e in entwurf.einwaende if e.offen]
        liste = [{"id": e.id, "art": "einwand", "rolle": e.rolle, "text": e.text} for e in offen]
        liste += [{"id": h.id, "art": "hinweis", "rolle": h.rolle, "text": h.text} for h in hinweise]
        d = self._rolle("ueberarbeitung", entwurf.familie,
                        {**basis, "entwurf.md": entwurf.hauptteil,
                         "einwaende.json": json.dumps(liste, ensure_ascii=False, indent=1)},
                        prompts.ueberarbeitung(self.a), "ueberarbeitung.json", schema_ueberarbeitung, runde=runde)
        entwurf.hauptteil = d["hauptteil"]
        nach_id = {e.id: e for e in [*offen, *hinweise]}
        zeilen = [f"### Runde {runde} – Überarbeitung Entwurf {entwurf.familie} ({entwurf.familie}, "
                  f"{self.k.modell('ueberarbeitung', entwurf.familie)})", ""]
        for antwort in d["antworten"]:
            e = nach_id.get(antwort["id"])
            if e is None:
                continue
            e.status, e.begruendung = antwort["status"], antwort["begruendung"]
            zeilen.append(f"- Antwort auf {e.id} ({e.rolle}): {e.status} – {einzeilig(e.begruendung) or '(ohne Begründung)'}")
        for e in offen:
            if e.offen:
                zeilen.append(f"- {e.id} ({e.rolle}): OFFEN – keine Antwort mit Status und Begründung")
        self._md("\n".join(zeilen) + "\n\nFassung nach Überarbeitung:\n\n" + entwurf.hauptteil)

    def gesamtnote(self, anwalt: float | None, richter: float, gegen: float) -> float:
        """Gewichtete Gesamtnote Σ wᵢ·noteᵢ / Σ wᵢ (Noten bereits validiert, kein Default)."""
        if anwalt is None:
            raise RolleFehlgeschlagen("schema", "review")
        w = self.k.gewichte
        return (w["anwalt"] * anwalt + w["richter"] * richter + w["gegenanwalt"] * gegen) / \
            (w["anwalt"] + w["richter"] + w["gegenanwalt"])

    def abgleich_sachverhalt(self, sv: dict[str, dict]) -> str:
        """Sachverhalte beider Familien zusammenführen; Quellen-Gate je Tatsache (TM-AC-3)."""
        gueltig = set(self.doks)
        ohne_beleg = 0
        luecken: list[str] = []
        herkunft = ", ".join(f"{f} {self.k.modell('sachverhalt', f)}" for f in FAMILIEN)
        z = [f"# Sachverhalt (Abgleich; Rolle sachverhalt: {herkunft})", ""]

        def belegt(quellen: list[str]) -> bool:
            return bool(quellen) and all(_DOK_RE.fullmatch(q) and q in gueltig for q in quellen)

        for f in FAMILIEN:
            z += [f"## Tatsachen (sachverhalt, {f}, {self.k.modell('sachverhalt', f)})", ""]
            for t in sv[f]["tatsachen"]:
                if belegt(t["quellen"]):
                    z.append(f"- [{f}:{einzeilig(t['id'], 40)}] {einzeilig(t['text'])} (Quellen: {', '.join(t['quellen'])})")
                else:
                    ohne_beleg += 1
                    z.append(f"- [{f}:{einzeilig(t['id'], 40)}] [OHNE BELEG] {einzeilig(t['text'])}")
                    luecken.append(f"Tatsache ohne gültigen Quellenverweis ({f}): {t['text']}")
            z.append("")
        # Chronologie: gruppiert nach Datum und Quellen; „beide“ nur, wenn beide Familien denselben
        # Text liefern, sonst steht der Text jeder Familie mit ihrer Kennzeichnung da.
        gruppen: dict[tuple[str, tuple[str, ...]], dict[str, list[str]]] = {}
        for f in FAMILIEN:
            for c in sv[f]["chronologie"]:
                key = (c["datum"], tuple(sorted(c["quellen"])))
                texte = gruppen.setdefault(key, {}).setdefault(f, [])
                if c["ereignis"] not in texte:
                    texte.append(c["ereignis"])
        z += ["## Chronologie", ""]
        for (datum, quellen), je_familie in sorted(gruppen.items()):
            normiert = {f: {re.sub(r"\s+", " ", t).strip().casefold() for t in ts} for f, ts in je_familie.items()}
            if len(je_familie) == len(FAMILIEN) and len({frozenset(v) for v in normiert.values()}) == 1:
                zeilen = [("beide", t) for t in je_familie[FAMILIEN[0]]]
            elif len(je_familie) == 1:
                f = next(iter(je_familie))
                zeilen = [(f"nur {f}", t) for t in je_familie[f]]
            else:
                zeilen = [(f"{f}, abweichend", t) for f in FAMILIEN for t in je_familie.get(f, [])]
            for marke, text in zeilen:
                if belegt(list(quellen)):
                    z.append(f"- {datum} ({marke}): {einzeilig(text)} (Quellen: {', '.join(quellen)})")
                else:
                    ohne_beleg += 1
                    z.append(f"- {datum} ({marke}): [OHNE BELEG] {einzeilig(text)}")
                    luecken.append(f"Chronologie-Eintrag {datum} ohne gültigen Quellenverweis")
        for f in FAMILIEN:
            luecken += sv[f]["luecken"]
        luecken = list(dict.fromkeys(luecken))
        z += ["", "## Lückenliste", ""] + ([f"- {einzeilig(l_)}" for l_ in luecken] or ["keine"])
        self.e.luecken = luecken
        if ohne_beleg:
            sec_event("legal.fact.unsourced", "flagged", count=ohne_beleg)
        return "\n".join(z) + "\n"

    def pruefe_rechtsfragen(self, text: str) -> str | None:
        """TM-AC-9: keine Pseudonym-Tokens, E-Mail, IBAN, Telefon; ≤ 8 KiB. Rückgabe: Verstoßkategorie."""
        if len(text.encode("utf-8")) > MAX_RECHTSFRAGEN:
            return "size"
        for kategorie, rx in (("pseudonym", self.k.pseudonym_re), ("email", _EMAIL_RE), ("iban", _IBAN_RE),
                              ("telefon", _TELEFON_RE)):
            if rx.search(text):
                return kategorie
        return None

    def abgleich_fristen(self, fr: dict[str, dict]) -> tuple[list[FristEintrag], list[str]]:
        """Fristen beider Familien zusammenführen; Abweichungen und früheste Frist für Timo (TM-AC-17)."""
        nach_key: dict[tuple[str, str], FristEintrag] = {}
        daten: dict[str, set[str]] = {f: set() for f in FAMILIEN}
        for f in FAMILIEN:
            for x in fr[f]["fristen"]:
                q = x["quelle"].strip()
                belegt = bool((_DOK_RE.fullmatch(q) and q in self.doks) or _NORM_RE.search(q))
                key = (x["datum"], re.sub(r"\s+", " ", x["bezeichnung"]).strip().casefold())
                daten[f].add(x["datum"])
                if key in nach_key:
                    alt = nach_key[key]
                    nach_key[key] = FristEintrag(alt.datum, alt.bezeichnung, alt.quelle or q, alt.belegt or belegt,
                                                 tuple(dict.fromkeys([*alt.familien, f])))
                else:
                    nach_key[key] = FristEintrag(x["datum"], x["bezeichnung"], q, belegt, (f,))
        fristen = sorted(nach_key.values(), key=lambda e: (e.datum, e.bezeichnung))
        for f in FAMILIEN:
            for d in sorted(daten[f] - daten[andere(f)]):
                bez = "; ".join(e.bezeichnung for e in fristen if e.datum == d and f in e.familien)
                self.e.frist_abweichungen.append(f"{d}: nur von {f} ermittelt ({bez})")
        self.e.frueheste_frist = fristen[0] if fristen else None
        risiken = list(dict.fromkeys(f"{r['art']}: {r['text']}" for f in FAMILIEN for r in fr[f]["risiken"]))
        herkunft = ", ".join(f"{f} {self.k.modell('fristen', f)}" for f in FAMILIEN)
        self._md(f"## Fristen und Risiken (Rolle fristen: {herkunft})\n\n" + ("\n".join(
            f"- {e.datum} {einzeilig(e.bezeichnung)} – Quelle {einzeilig(e.quelle) or '–'}"
            f"{'' if e.belegt else ' [UNGEPRÜFT]'} (fristen: "
            + ", ".join(f"{f} {self.k.modell('fristen', f)}" for f in e.familien) + ")"
            for e in fristen) or "keine Frist erkennbar")
            + ("\n\nAbweichungen:\n" + "\n".join(f"- {x}" for x in self.e.frist_abweichungen)
               if self.e.frist_abweichungen else ""))
        return fristen, risiken

    def _abschluss(self, sieger: Entwurf, fristen: list[FristEintrag], risiken: list[str],
                   vergleich: dict | None, pruefer: str) -> int:
        """Abschluss-Gates: Zitat, Wortlimit, Pflichtabschnitte, offene Einwände, Strategie-Leak."""
        a = self.a
        gruende: list[str] = []
        hauptteil = entschaerfe_ueberschriften(bereinige_markdown(sieger.hauptteil, self.k.research_domains))
        woerter = zaehle_woerter(hauptteil)
        zp = zitat_gate(hauptteil, self.register)
        for w in zp.widersprueche:
            self.e.widersprueche.append(w)
        if zp.widersprueche:
            self._md("## Widerspruch (Rolle Researcher/Register, Host-Zitat-Gate)\n\n"
                     + "\n".join(f"- {w}" for w in zp.widersprueche))
            gruende.append(f"Zitat-Gate: {len(zp.widersprueche)} Zitat(e) ohne gültigen Registereintrag")
        ungepruefte = [f"{z} – {e.gericht}, {e.datum}, Status ungeprüft: Fundstelle nur vom Researcher genannt, "
                       f"nicht durch die andere Familie bestätigt" if z in zp.nur_zitat else
                       f"{z} – {e.gericht}, {e.datum}, Status {e.status}: {e.grund}"
                       for z, e in [*zp.ungeprueft, *zp.haelt_nicht]]
        ungepruefte += [f"{z} – kein Registereintrag" for z in zp.unregistriert]
        self.e.ungepruefte = ungepruefte
        markiert = markiere_ungepruefte(hauptteil, [z for z, _ in [*zp.ungeprueft, *zp.haelt_nicht]] + zp.unregistriert)
        if a.wortlimit and woerter > a.wortlimit:
            sec_event("legal.wordlimit.exceeded", "rejected", woerter=woerter, limit=a.wortlimit)
            gruende.append(f"Wortlimit überschritten: {woerter} > {a.wortlimit}")
        offen = [e for e in sieger.einwaende if e.offen]
        if offen:
            sec_event("legal.objection.open", "rejected", count=len(offen))
            gruende.append(f"{len(offen)} Einwand/Einwände ohne Status und Begründung")
        self.e.offene_einwaende = [f"{e.id} ({e.rolle}, {e.familie}): {e.text}" for e in offen]
        self.e.abgelehnte_einwaende = [f"{e.id} ({e.rolle}, {e.familie}): {e.text} – Begründung: {e.begruendung}"
                                       for e in sieger.einwaende if e.status == "abgelehnt"]
        if vergleich is not None:
            schreibe_datei(self.ergebnis / "vergleich_intern.md",
                           bereinige_markdown(baue_vergleich_intern(vergleich, pruefer, self.k.modell("stratege", pruefer)),
                                              self.k.research_domains, komponente="vergleich"))
            if ist_schreiben(a.ergebnis) and strategie_leak(hauptteil, (vergleich["spanne_min"], vergleich["spanne_max"])):
                sec_event("legal.strategy.leak", "rejected")
                gruende.append("Betrag der internen Vergleichsspanne steht im Schreiben")
        final = not gruende

        def _beratung(ist_final: bool) -> str:
            # Gesamte Datei bereinigen: auch Anhang-Felder (Fristen, Risiken, Register) sind Modelltext.
            return bereinige_markdown(
                baue_beratung(ergebnis_art=a.ergebnis, hauptteil=markiert, fristen=fristen, risiken=risiken,
                              ungepruefte=ungepruefte, final=ist_final), self.k.research_domains)

        beratung = _beratung(final)
        fehlend = pruefe_pflichtabschnitte(beratung)
        if fehlend:
            gruende.append("Pflichtabschnitte fehlen: " + ", ".join(fehlend))
            final = False
            beratung = _beratung(False)
        schreibe_datei(self.ergebnis / "beratung.md", beratung)
        self._md(f"## Gewählte Endfassung ({sieger.familie}, Mandanten-Anwalt {self.k.modell('anwalt', sieger.familie)})"
                 f"\n\nWortzahl Hauptteil (Host): {woerter}")
        self.e.gruende_nicht_final += gruende
        progress(f"Lauf beendet: {'final' if final else 'nicht final'} ({self.aufrufe} Modellaufrufe)")
        return EXIT_FINAL if final else EXIT_NICHT_FINAL
