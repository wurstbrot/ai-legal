"""Strukturiertes Logging ohne Akteninhalte (TM-AC-21, TM-AC-22).

Security-relevante Ereignisse werden mit `[SEC]` getaggt und tragen Zeitstempel,
Subjekt (OS-Nutzer und Lauf-ID), Komponente und Ergebnis. Logs enthalten nie
Prompt-, Akten- oder Modellinhalte, keine Dateinamen aus `fall/` und keine
Credentials. Alle Werte werden gegen Log- und Terminal-Injection bereinigt.
"""
from __future__ import annotations

import datetime
import getpass
import logging
import os
import re
import sys
from typing import Any

LOGGER_NAME = "legal"

# ANSI/VT-Sequenzen: CSI (ESC [ … Finalbyte), OSC (ESC ] … BEL oder ESC \),
# DCS/SOS/PM/APC (ESC P|X|^|_ … ESC \) sowie einzelne ESC-Zweizeichenfolgen.
# Zusätzlich die 8-Bit-Varianten CSI (U+009B) und OSC (U+009D).
_ANSI_RE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)?"
    r"|\x1b[@-Z\\-_]?"
)
# Alle übrigen Steuerzeichen außer \n und \t (C0, DEL, C1) sowie U+2028/U+2029
# und bidirektionale Steuerzeichen, die Text im Terminal umsortieren können.
_STEUER_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")

_LOG_MAX = 300

_lauf_id = "-"


def terminal_sicher(text: Any) -> str:
    """Steuerzeichen und Escape-Sequenzen entfernen; nur `\\n` und `\\t` bleiben (TM-AC-22)."""
    s = str(text).replace("\r\n", "\n")
    s = _ANSI_RE.sub("", s)
    return _STEUER_RE.sub("", s)


def bereinige_log(value: Any) -> str:
    """Einen Log-Wert einzeilig und ohne Steuerzeichen machen (Log-Injection-Schutz)."""
    s = terminal_sicher(value).replace("\n", " ").replace("\t", " ")
    if len(s) > _LOG_MAX:
        s = s[:_LOG_MAX] + "…"
    return s


def setze_lauf_id(lauf_id: str) -> None:
    """Lauf-ID für das Subjekt aller folgenden `[SEC]`-Events setzen."""
    global _lauf_id
    _lauf_id = bereinige_log(lauf_id)


def _os_nutzer() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return f"uid{os.getuid()}"


def get_logger() -> logging.Logger:
    """Logger `legal`; Level aus `LEGAL_LOG_LEVEL` (Default INFO), Ausgabe auf stderr."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        level = os.environ.get("LEGAL_LOG_LEVEL", "INFO").strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"
        logger.setLevel(level)
    return logger


def progress(message: str) -> None:
    """Fortschrittsmeldung ohne `[SEC]`-Tag. Nie Inhalte aus Akte, Prompt oder Modell übergeben."""
    get_logger().info(bereinige_log(message))


def sec_event(event: str, outcome: str, *, komponente: str = "conductor", **felder: Any) -> None:
    """Ein `[SEC]`-Ereignis protokollieren (TM-AC-21).

    `felder` dürfen nur nicht-sensitive Metadaten enthalten (Zähler, Kategorien,
    Rollen, Indizes, Dauer) – niemals Inhalte, Dateinamen aus `fall/` oder Secrets.
    """
    zeit = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        f"[SEC] ts={zeit} event={bereinige_log(event)} outcome={bereinige_log(outcome)}",
        f"user={bereinige_log(_os_nutzer())} run={_lauf_id} component={bereinige_log(komponente)}",
    ]
    for key, val in felder.items():
        parts.append(f"{bereinige_log(key)}={bereinige_log(val)}")
    level = logging.WARNING if outcome in ("aborted", "denied", "rejected") else logging.INFO
    get_logger().log(level, " ".join(parts))
