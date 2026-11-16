#!/usr/bin/env bash
# Wrapper für `legal`: ruft den Python-Einstieg aus dem Repository-Wurzelverzeichnis auf.
# Aufruf: legal.bash [fallordner] [--keep]
# Ohne fallordner gilt das aktuelle Verzeichnis des Aufrufers als Fall-Ordner.
# Ein relativer fallordner bezieht sich auf das Verzeichnis des Aufrufers; legal.py
# löst ihn gegen LEGAL_AUFRUF_CWD auf (der Wrapper wechselt ins Repository).
set -euo pipefail
export LEGAL_AUFRUF_CWD="$PWD"
# Symlinks (z. B. ~/bin/legal -> legal.bash) auflösen, damit das Repository gefunden wird.
skript="$(readlink -f "${BASH_SOURCE[0]}")"
cd "$(dirname "$skript")/.." && exec python3 -m rechtsberatung.legal "$@"
