"""Konfiguration für `legal` aus Umgebungsvariablen (TM-AC-15, -19, -20).

Reine Funktionen: Die Umgebung wird erst beim Aufruf gelesen, es gibt keine
Seiteneffekte beim Import. Ungültige Werte führen zu `KonfigFehler` (Exit 2),
bevor irgendein Modell aufgerufen wird.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

from .logging_setup import sec_event

FAMILIEN = ("claude", "codex")

# Rollen des Conductors (Schlüssel für Modell-Overrides `LEGAL_<FAMILIE>_MODEL_<ROLLE>`).
ROLLEN = (
    "sachverhalt", "rechtsfragen", "researcher", "registercheck", "anwalt", "review",
    "ueberarbeitung", "richter", "gegenanwalt", "persona_klaeger", "persona_beklagter",
    "stratege", "groesse", "coldread",
)

CLAUDE_MODEL_DEFAULT = "claude-fable-5-2"
CODEX_DEFAULT_LABEL = "codex-default"

MODELL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,63}$")
IMAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/\-]{0,127}(?::[A-Za-z0-9_][A-Za-z0-9._\-]{0,127})?(?:@sha256:[a-f0-9]{64})?$"
)
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

LLM_DOMAINS_DEFAULT = ("api.anthropic.com", "api.openai.com", "chatgpt.com", "auth.openai.com")
# E-4 (geändert 2026-11-16, Entscheidung Timo): keine Netz-Allowlist mehr für die Recherche.
# Die Liste nennt bevorzugte Quellen im Researcher-Prompt und die Domains, deren Links im
# Ergebnis klickbar bleiben; Links auf andere Domains erscheinen entschärft als Text.
RESEARCH_DOMAINS_DEFAULT = (
    "rechtsprechung-im-internet.de", "gesetze-im-internet.de", "bundesgerichtshof.de",
    "bundesverfassungsgericht.de", "bundesarbeitsgericht.de", "bundessozialgericht.de",
    "bundesfinanzhof.de", "bverwg.de", "dejure.org", "openjur.de", "landesrecht-hamburg.de",
    "gesetze-rechtsprechung.sh.juris.de",
)
PSEUDONYM_REGEX_DEFAULT = r"<[A-ZÄÖÜ_]+_?\d+>|\b[A-ZÄÖÜ]{3,}_\d+\b"

GEWICHTE_DEFAULT = {"anwalt": 1.0, "richter": 0.5, "gegenanwalt": 0.5}

# Verbotene Dateinamen in Mounts (TM-AC-2, TM-AC-4).
MAPPING_NAMEN_DEFAULT = ("mapping.md",)
VERBOTENE_MOUNT_NAMEN = (
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude", ".codex", ".mcp.json", ".git",
)


class KonfigFehler(ValueError):
    """Ungültige Konfiguration (führt zu Exit 2 vor jedem Modellaufruf)."""


@dataclass(frozen=True)
class Budget:
    """Grenzen des Laufs (E-10)."""

    max_runden: int = 3
    max_aufrufe: int = 60
    timeout_s: int = 1800
    max_laufzeit_s: int = 14400
    review_runden: int = 1


@dataclass(frozen=True)
class Konfig:
    """Validierte Konfiguration eines Laufs."""

    claude_modelle: Mapping[str, str]
    codex_modelle: Mapping[str, str]
    gewichte: Mapping[str, float]
    budget: Budget
    image: str
    llm_domains: tuple[str, ...]
    research_domains: tuple[str, ...]
    pseudonym_re: re.Pattern[str]
    mapping_namen: tuple[str, ...]
    overrides: tuple[str, ...] = field(default=())

    def modell(self, rolle: str, familie: str) -> str:
        """Modell-ID für Rolle und Familie; Codex ohne Override liefert `codex-default`."""
        if familie == "claude":
            return self.claude_modelle[rolle]
        if familie == "codex":
            return self.codex_modelle[rolle] or CODEX_DEFAULT_LABEL
        raise KonfigFehler("unbekannte Modellfamilie")

    def modell_argument(self, rolle: str, familie: str) -> str:
        """Modell-ID für den CLI-Aufruf; leer bedeutet CLI-Default (nur Codex)."""
        if familie == "claude":
            return self.claude_modelle[rolle]
        return self.codex_modelle[rolle]


def _env(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _modell(env: Mapping[str, str], familie: str, rolle: str,
            default: str, overrides: list[str]) -> str:
    prefix = f"LEGAL_{familie.upper()}_MODEL"
    spez = _env(env, f"{prefix}_{rolle.upper()}")
    allg = _env(env, prefix)
    wert = spez or allg or default
    if wert and not MODELL_RE.fullmatch(wert):
        raise KonfigFehler(f"ungültige Modell-ID in {prefix}[_{rolle.upper()}]")
    if spez or allg:
        overrides.append(f"{familie}:{rolle}")
    return wert


def _ganzzahl(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    roh = _env(env, name)
    if not roh:
        return default
    if not re.fullmatch(r"\d{1,9}", roh):
        raise KonfigFehler(f"{name} ist keine Ganzzahl")
    wert = int(roh)
    if not lo <= wert <= hi:
        raise KonfigFehler(f"{name} außerhalb {lo}–{hi}")
    return wert


def _gewicht(env: Mapping[str, str], rolle: str) -> float:
    name = f"LEGAL_GEWICHT_{rolle.upper()}"
    roh = _env(env, name)
    if not roh:
        return GEWICHTE_DEFAULT[rolle]
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,6})?", roh):
        raise KonfigFehler(f"{name} ist keine Zahl")
    wert = float(roh)
    if not (math.isfinite(wert) and 0.0 < wert <= 1.0):
        raise KonfigFehler(f"{name} muss in (0, 1] liegen")
    return wert


def _domains(env: Mapping[str, str], name: str, basis: tuple[str, ...], ersetzen: bool) -> tuple[str, ...]:
    roh = _env(env, name)
    extra = [d.strip().lower() for d in roh.split(",") if d.strip()] if roh else []
    for d in extra:
        if not DOMAIN_RE.fullmatch(d):
            raise KonfigFehler(f"ungültige Domain in {name}")
    liste = extra if (ersetzen and extra) else [*basis, *extra]
    return tuple(dict.fromkeys(liste))


def lade_konfig(env: Mapping[str, str] | None = None) -> Konfig:
    """Konfiguration aus der Umgebung lesen und vollständig validieren."""
    env = os.environ if env is None else env
    overrides: list[str] = []
    claude = {r: _modell(env, "claude", r, CLAUDE_MODEL_DEFAULT, overrides) for r in ROLLEN}
    codex = {r: _modell(env, "codex", r, "", overrides) for r in ROLLEN}
    gewichte = {r: _gewicht(env, r) for r in GEWICHTE_DEFAULT}
    budget = Budget(
        max_runden=_ganzzahl(env, "LEGAL_MAX_RUNDEN", 3, 1, 20),
        max_aufrufe=_ganzzahl(env, "LEGAL_MAX_AUFRUFE", 60, 1, 1000),
        timeout_s=_ganzzahl(env, "LEGAL_TIMEOUT_S", 1800, 10, 86400),
        max_laufzeit_s=_ganzzahl(env, "LEGAL_MAX_LAUFZEIT_S", 14400, 60, 7 * 86400),
        review_runden=_ganzzahl(env, "LEGAL_REVIEW_RUNDEN", 1, 1, 10),
    )
    image = _env(env, "LEGAL_IMAGE") or "legal:1"
    if not IMAGE_RE.fullmatch(image):
        raise KonfigFehler("ungültiger Image-Name in LEGAL_IMAGE")
    llm = _domains(env, "LEGAL_LLM_DOMAINS", LLM_DOMAINS_DEFAULT, ersetzen=True)
    research = _domains(env, "LEGAL_RESEARCH_DOMAINS", RESEARCH_DOMAINS_DEFAULT, ersetzen=False)
    pseudo_roh = _env(env, "LEGAL_PSEUDONYM_REGEX") or PSEUDONYM_REGEX_DEFAULT
    if len(pseudo_roh) > 500:
        raise KonfigFehler("LEGAL_PSEUDONYM_REGEX zu lang")
    try:
        pseudo = re.compile(pseudo_roh)
    except re.error as exc:
        raise KonfigFehler("LEGAL_PSEUDONYM_REGEX ist kein gültiger regulärer Ausdruck") from exc
    if pseudo.search("") is not None:
        # Ein Muster, das die leere Zeichenkette trifft, würde jede Rechtsfrage abweisen.
        raise KonfigFehler("LEGAL_PSEUDONYM_REGEX trifft die leere Zeichenkette")
    mapping_extra = [n.strip() for n in _env(env, "LEGAL_MAPPING_NAMEN").split(",") if n.strip()]
    for n in mapping_extra:
        if not re.fullmatch(r"[A-Za-z0-9._\-]{1,100}", n):
            raise KonfigFehler("ungültiger Dateiname in LEGAL_MAPPING_NAMEN")
    mapping = tuple(dict.fromkeys([*MAPPING_NAMEN_DEFAULT, *mapping_extra]))
    for o in overrides:
        familie, rolle = o.split(":", 1)
        sec_event("legal.model.override", "ok", komponente="config", familie=familie, rolle=rolle)
    return Konfig(
        claude_modelle=claude, codex_modelle=codex, gewichte=gewichte, budget=budget,
        image=image, llm_domains=llm, research_domains=research, pseudonym_re=pseudo,
        mapping_namen=mapping, overrides=tuple(overrides),
    )


def domain_erlaubt(host: str, domains: tuple[str, ...]) -> bool:
    """True, wenn `host` exakt einer Domain der Liste entspricht oder deren Subdomain ist."""
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in domains)
