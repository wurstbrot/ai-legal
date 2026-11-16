"""Unit-Tests für `rechtsberatung` (unittest, ohne Docker).

Die Konsolenausgabe des Loggers wird in Tests gedämpft; `assertLogs` erfasst die
Ereignisse trotzdem vollständig.
"""
import os

os.environ.setdefault("LEGAL_LOG_LEVEL", "ERROR")
