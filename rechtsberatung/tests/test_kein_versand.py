"""TM-AC-26: `rechtsberatung/` enthält keinen Versand-Code (kein smtplib, kein HTTP-POST)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PAKET = Path(__file__).resolve().parents[1]
VERBOTEN = re.compile(
    r"smtplib|requests\.post|httpx\.post|http\.client|urlopen\s*\(|urllib\.request|"
    r"method\s*[=:]\s*['\"]POST['\"]|['\"]POST['\"]|sendmail|\bbea\b",
    re.IGNORECASE,
)


class KeinVersandTest(unittest.TestCase):
    def test_kein_versand_code(self) -> None:
        funde = []
        for pfad in sorted([*PAKET.rglob("*.py"), *PAKET.rglob("*.js"), *PAKET.rglob("*.bash")]):
            if "tests" in pfad.relative_to(PAKET).parts or pfad.name.endswith(".test.js"):
                continue
            for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), start=1):
                if VERBOTEN.search(zeile):
                    funde.append(f"{pfad.relative_to(PAKET)}:{nr}")
        self.assertEqual(funde, [])

    def test_muster_greift(self) -> None:
        for beispiel in ("import smtplib", "requests.post(url)", "urllib.request.urlopen(req)",
                         "fetch(u, {method: 'POST'})"):
            self.assertRegex(beispiel, VERBOTEN)

    def test_kein_prompt_als_argument_und_keine_shell(self) -> None:
        for pfad in PAKET.glob("*.py"):
            text = pfad.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, pfad.name)
            self.assertNotIn("os.system", text, pfad.name)
            self.assertNotIn("--dangerously-skip-permissions", text, pfad.name)


if __name__ == "__main__":
    unittest.main()
