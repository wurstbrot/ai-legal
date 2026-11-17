"""Diagnose-Hilfe: Fehler und Warnungen aus einem Codex-Home eines Laufs, ohne Akteninhalte.

Aufruf (Arbeitsverzeichnis muss noch existieren, z. B. mit `--keep` oder während des Laufs):
    python3 -I rechtsberatung/diagnose_codex.py /tmp/legal-XXXX/002_sachverhalt_codex/home/.codex [--details]

`--details` zeigt zusätzlich die kurzen Felder der letzten Ereignisse (Befehle der Tool-Aufrufe,
Abbruchgrund, Anfang der letzten Modellnachricht). Das kann pseudonymisierten Akteninhalt enthalten.

Nur lesend (SQLite `mode=ro`). Ausgegeben werden Log-Zeilen mit ERROR/WARN/panic sowie Typ und
Fehlerfelder der letzten Session-Ereignisse, je höchstens 400 Zeichen. Wird nicht von `legal`
aufgerufen und sendet nichts.
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
import sys

_MUSTER = re.compile(r"\b(ERROR|WARN|panic|panicked|fatal)\b", re.I)
_TABELLE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_MAX = 400


def logs(home: str) -> None:
    for db in sorted(glob.glob(f"{home}/logs_*.sqlite")):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            try:
                stufen = con.execute("select level, count(*) from logs group by level").fetchall()
                print("LOG-STUFEN", db.rsplit("/", 1)[-1], stufen)
            except sqlite3.Error:
                pass
            for (tabelle,) in con.execute("select name from sqlite_master where type='table'"):
                if not _TABELLE.fullmatch(tabelle):
                    continue
                spalten = [r[1] for r in con.execute(f"pragma table_info('{tabelle}')")]  # nosec B608
                print("TABELLE", db.rsplit("/", 1)[-1], tabelle, spalten)
                treffer = []
                for zeile in con.execute(f"select * from '{tabelle}' order by rowid desc limit 2000"):  # nosec B608
                    text = " | ".join(str(x) for x in zeile)
                    if _MUSTER.search(text):
                        treffer.append(text[:_MAX])
                for text in reversed(treffer[:60]):
                    print("  ", text)
        finally:
            con.close()


def _kurz(v: object, n: int = 300) -> str:
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    return " ".join(str(v).split())[:n]


def _details(p: dict) -> str:
    teile = []
    for k, v in p.items():
        if k in ("type", "name", "status", "encrypted_content", "summary"):
            continue
        if k == "content" and isinstance(v, list):
            texte = [x.get("text", "") for x in v if isinstance(x, dict)]
            teile.append(f"content={_kurz(' '.join(texte))}")
        else:
            teile.append(f"{k}={_kurz(v, 200)}")
    return "\n        ".join(teile)


def session(home: str, details: bool = False) -> None:
    dateien = sorted(glob.glob(f"{home}/sessions/**/*.jsonl", recursive=True))
    if not dateien:
        print("keine Session-Datei")
        return
    with open(dateien[-1], encoding="utf-8") as fh:
        zeilen = fh.read().splitlines()
    print(f"\n== Ende der Session: {len(zeilen)} Zeilen ==")
    for roh in zeilen[-15:]:
        try:
            d = json.loads(roh)
        except ValueError:
            print("   (kein JSON)")
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        art = (p.get("type", ""), p.get("name", ""), p.get("status", ""))
        fehler = ""
        for k, v in p.items():
            if "error" in k.lower() or (isinstance(v, str) and len(v) < 2000 and "error" in v.lower()):
                fehler = f"{k}={str(v)[:300]}"
                break
        print("  ", d.get("timestamp", ""), d.get("type"), art, fehler)
        if details and art[0] != "reasoning":
            print("        " + _details(p))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--details"]
    if len(args) != 1:
        sys.exit("Aufruf: diagnose_codex.py <codex-home> [--details]")
    h = args[0].rstrip("/")
    logs(h)
    session(h, details="--details" in sys.argv[1:])
