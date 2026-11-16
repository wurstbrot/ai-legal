#!/usr/bin/env bash
# Wrapper für `legal`: ruft den Python-Einstieg aus dem Repository-Wurzelverzeichnis auf.
# Aufruf: legal.bash <fallordner> [--keep]
# Ein relativer <fallordner> bezieht sich auf das Verzeichnis des Aufrufers; legal.py
# löst ihn gegen LEGAL_AUFRUF_CWD auf (der Wrapper wechselt ins Repository).
set -euo pipefail
export LEGAL_AUFRUF_CWD="$PWD"
cd "$(dirname "$0")/.." && exec python3 -m rechtsberatung.legal "$@"
