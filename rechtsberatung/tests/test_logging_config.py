"""Tests für Logging (TM-AC-21, -22) und Konfiguration (TM-AC-15, -19, -20)."""
from __future__ import annotations

import unittest

from rechtsberatung import config
from rechtsberatung.config import KonfigFehler, lade_konfig
from rechtsberatung.logging_setup import bereinige_log, sec_event, terminal_sicher


class LoggingTest(unittest.TestCase):
    def test_osc52_und_ansi_werden_entfernt(self) -> None:
        roh = "a\x1b]52;c;SGFjaw==\x07b\x1b[31mrot\x1b[0m\x9b2Jc\x9d0;titel\x07d"
        s = terminal_sicher(roh)
        self.assertNotIn("\x1b", s)
        self.assertNotIn("52;c", s)
        self.assertNotIn("\x9b", s)
        self.assertEqual(s, "abrotcd")

    def test_steuerzeichen_ausser_newline_tab(self) -> None:
        s = terminal_sicher("x\u0085y\u2028z\u2029\x00\x07\x7f\tq\nr\u202eS")
        self.assertEqual(s, "xyz\tq\nrS")

    def test_log_wert_einzeilig(self) -> None:
        self.assertEqual(bereinige_log("a\r\nb\nc\td"), "a b c d")

    def test_sec_event_format_ohne_injection(self) -> None:
        with self.assertLogs("legal", level="INFO") as cm:
            sec_event("legal.test", "ok", feld="x\n[SEC] event=gefaelscht")
        zeile = cm.output[0]
        self.assertEqual(zeile.count("\n"), 0)
        for teil in ("[SEC] ts=", "event=legal.test", "outcome=ok", "user=", "run=", "component=conductor"):
            self.assertIn(teil, zeile)


class KonfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        k = lade_konfig({})
        self.assertEqual(k.modell("anwalt", "claude"), "claude-fable-5-2")
        self.assertEqual(k.modell("anwalt", "codex"), config.CODEX_DEFAULT_LABEL)
        self.assertEqual(k.modell_argument("anwalt", "codex"), "")
        self.assertEqual(dict(k.gewichte), {"anwalt": 1.0, "richter": 0.5, "gegenanwalt": 0.5})
        self.assertEqual((k.budget.max_runden, k.budget.max_aufrufe, k.budget.timeout_s, k.budget.max_laufzeit_s,
                          k.budget.review_runden), (3, 60, 1800, 14400, 1))
        self.assertEqual(k.image, "legal:1")
        self.assertIn("dejure.org", k.research_domains)
        self.assertNotIn("dejure.org", k.llm_domains)

    def test_rollen_override_und_sec_event(self) -> None:
        with self.assertLogs("legal", level="INFO") as cm:
            k = lade_konfig({"LEGAL_CLAUDE_MODEL_RICHTER": "claude-opus-5-5", "LEGAL_CODEX_MODEL": "gpt-5.5"})
        self.assertEqual(k.modell("richter", "claude"), "claude-opus-5-5")
        self.assertEqual(k.modell("anwalt", "claude"), "claude-fable-5-2")
        self.assertEqual(k.modell("anwalt", "codex"), "gpt-5.5")
        self.assertTrue(any("event=legal.model.override" in z for z in cm.output))

    def test_ungueltige_modell_ids(self) -> None:
        for wert in ("claude;rm", "Claude-X", "-claude", "a", "x" * 70, "claude model", "c$(id)"):
            with self.subTest(wert=wert), self.assertRaises(KonfigFehler):
                lade_konfig({"LEGAL_CLAUDE_MODEL": wert})
        with self.assertRaises(KonfigFehler):
            lade_konfig({"LEGAL_CODEX_MODEL_COLDREAD": "gpt;id"})

    def test_ungueltige_gewichte(self) -> None:
        for wert in ("0", "1.5", "-0.5", "nan", "inf", "abc"):
            with self.subTest(wert=wert), self.assertRaises(KonfigFehler):
                lade_konfig({"LEGAL_GEWICHT_RICHTER": wert})
        self.assertEqual(lade_konfig({"LEGAL_GEWICHT_RICHTER": "0.25"}).gewichte["richter"], 0.25)

    def test_ungueltiges_budget_image_domain_regex(self) -> None:
        faelle = [
            {"LEGAL_MAX_AUFRUFE": "0"}, {"LEGAL_MAX_AUFRUFE": "x"}, {"LEGAL_TIMEOUT_S": "-1"},
            {"LEGAL_MAX_RUNDEN": "999"}, {"LEGAL_IMAGE": "legal:1;rm -rf"}, {"LEGAL_IMAGE": "Legal"},
            {"LEGAL_RESEARCH_DOMAINS": "evil.com/x"}, {"LEGAL_LLM_DOMAINS": "*.com"},
            {"LEGAL_PSEUDONYM_REGEX": "("}, {"LEGAL_MAPPING_NAMEN": "../x"},
        ]
        for env in faelle:
            with self.subTest(env=env), self.assertRaises(KonfigFehler):
                lade_konfig(env)

    def test_pseudonym_regex_trifft_leere_zeichenkette(self) -> None:
        for muster in ("a*", "^", "(x)?", ""):
            with self.subTest(muster=muster):
                if muster == "":
                    self.assertIsNotNone(lade_konfig({"LEGAL_PSEUDONYM_REGEX": muster}).pseudonym_re)  # Default
                else:
                    with self.assertRaises(KonfigFehler):
                        lade_konfig({"LEGAL_PSEUDONYM_REGEX": muster})

    def test_domain_erlaubt(self) -> None:
        d = ("dejure.org",)
        self.assertTrue(config.domain_erlaubt("www.dejure.org", d))
        self.assertFalse(config.domain_erlaubt("evildejure.org", d))
        self.assertFalse(config.domain_erlaubt("dejure.org.evil.com", d))


if __name__ == "__main__":
    unittest.main()
