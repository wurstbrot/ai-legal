"""Fortsetzen eines früheren Laufs (`--fortsetzen`, Entscheidung Timo 2026-11-17)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from rechtsberatung.tests.fake import erstelle_fall
from rechtsberatung.tests.test_conductor import Basis


class _FortsetzungBasis(Basis):
    def lauf(self, **kw):  # type: ignore[override]
        # Zwei Läufe in derselben Sekunde: den neuen Ergebnisordner gezielt wählen (nicht nach Name).
        vorher = set((kw["root"] / "ergebnis").iterdir()) if kw.get("root") and (kw["root"] / "ergebnis").exists() \
            else set()
        l_ = super().lauf(**kw)
        neu = [p for p in (l_.root / "ergebnis").iterdir() if p not in vorher] if (l_.root / "ergebnis").exists() \
            else []
        l_.dir = neu[0] if len(neu) == 1 else None
        return l_

    def _erstlauf(self, name: str = "fs") -> tuple[Path, Path]:
        root = erstelle_fall(self.tmp / name)
        erst = self.lauf(root=root)
        self.assertEqual(erst.code, 0)
        self.assertTrue(erst.runner.aufrufe)
        return root, erst.dir


class FortsetzungTest(_FortsetzungBasis):
    def test_unveraenderter_fall_ohne_modellaufruf(self) -> None:
        root, alt = self._erstlauf()
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertEqual(l_.code, 0, l_.datei("entscheidungen.md"))
        self.assertEqual(l_.runner.aufrufe, [])
        self.assertTrue(l_.event("legal.container.reused"))
        self.assertFalse(l_.event("legal.container.start"))
        container = [e for e in l_.jsonl() if e.get("typ") == "container"]
        self.assertTrue(container)
        self.assertTrue(all(e.get("wiederverwendet_aus") == alt.name for e in container))
        self.assertIn(f"Fortsetzung aus Lauf: {alt.name}", l_.datei("protokoll.md"))
        # Die neue Ausgabe liegt im neuen Lauf und ist inhaltlich identisch.
        self.assertTrue(any((l_.dir / p.name).is_dir() for p in alt.iterdir() if p.is_dir()))

    def test_explizite_lauf_id(self) -> None:
        root, alt = self._erstlauf()
        l_ = self.lauf(root=root, argv=["--fortsetzen", alt.name])
        self.assertEqual(l_.runner.aufrufe, [])

    def test_geaenderte_akte_rechnet_betroffene_rollen_neu(self) -> None:
        root, _ = self._erstlauf()
        (root / "01_mail.txt").write_text("Neuer Inhalt <NAME_1> am 2026-01-02.", encoding="utf-8")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        rollen = {r for r, _ in l_.runner.rollen()}
        self.assertIn("sachverhalt", rollen)     # Akte ist Eingabe
        self.assertIn("anwalt", rollen)          # Akte ist Eingabe
        self.assertNotIn("rechtsfragen", rollen)  # nur Sachverhalt (unverändert) und Anweisung

    def test_geaenderte_anweisung_rechnet_alles_neu(self) -> None:
        root, _ = self._erstlauf()
        (root / "anweisung.md").write_text("Ergebnis: Einschätzung\nSeite: Beklagter\n", encoding="utf-8")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertIn("sachverhalt", {r for r, _ in l_.runner.rollen()})

    def test_manipulierte_ausgabe_wird_nicht_uebernommen(self) -> None:
        root, alt = self._erstlauf()
        datei = next(alt.glob("*_rechtsfragen_claude/rechtsfragen.json"))
        datei.write_text(json.dumps({"fragen": ["MANIPULIERT"], "normen": []}), encoding="utf-8")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertIn("rechtsfragen", {r for r, _ in l_.runner.rollen()})
        self.assertTrue(l_.event("legal.resume.mismatch"))
        self.assertNotIn("MANIPULIERT", l_.datei("protokoll.md"))

    def test_symlink_als_ausgabe_wird_nicht_gelesen(self) -> None:
        root, alt = self._erstlauf()
        datei = next(alt.glob("*_rechtsfragen_claude/rechtsfragen.json"))
        geheim = self.tmp / "geheim.json"
        geheim.write_bytes(datei.read_bytes())
        datei.unlink()
        os.symlink(geheim, datei)
        # Der Fallordner ist gemountet: das Mount-Gate bricht vor jedem Container ab (Symlink im Baum).
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertEqual(l_.code, 2)
        self.assertEqual(l_.runner.aufrufe, [])
        self.assertTrue(l_.event("legal.mount.unsafe"))
        self.assertFalse(l_.event("legal.container.reused"))

    def test_uebernommene_ausgabe_wird_erneut_geprueft(self) -> None:
        # Ausgabe UND protokollierter Hash konsistent manipuliert (schemawidrig): Prüfung lehnt ab.
        root, alt = self._erstlauf()
        datei = next(alt.glob("*_rechtsfragen_claude/rechtsfragen.json"))
        neu = json.dumps({"fragen": ["x"], "normen": [], "freigabe": True}).encode()
        alt_hash = hashlib.sha256(datei.read_bytes()).hexdigest()
        datei.write_bytes(neu)
        prot = alt / "protokoll.jsonl"
        prot.write_text(prot.read_text(encoding="utf-8").replace(alt_hash, hashlib.sha256(neu).hexdigest()),
                        encoding="utf-8")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertEqual(l_.code, 3)
        self.assertTrue(any("event=legal.output.rejected" in z and "rolle=rechtsfragen" in z for z in l_.logs))

    def test_ungueltige_quelle_bricht_vor_jedem_aufruf_ab(self) -> None:
        root, _ = self._erstlauf()
        for wunsch in ("../../etc", "20990101-000000-deadbeef", "x" * 300):
            with self.subTest(wunsch=wunsch[:20]):
                l_ = self.lauf(root=root, argv=["--fortsetzen", wunsch])
                self.assertEqual(l_.code, 2)
                self.assertEqual(l_.runner.aufrufe, [])
                self.assertTrue(l_.event("legal.resume.rejected"))

    def test_ohne_frueheren_lauf(self) -> None:
        root = erstelle_fall(self.tmp / "leer")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertEqual(l_.code, 2)
        self.assertEqual(l_.runner.aufrufe, [])

    def test_gefaelschte_protokolleintraege_werden_ignoriert(self) -> None:
        root, alt = self._erstlauf()
        prot = alt / "protokoll.jsonl"
        zeilen = [json.loads(z) for z in prot.read_text(encoding="utf-8").splitlines()]
        boese = []
        for z in zeilen:
            if z.get("typ") == "container":
                z = {**z, "rolle": "../../x", "familie": "evil"}
            boese.append(json.dumps(z))
        prot.write_text("\n".join(boese + ["kein json", "[]"]) + "\n", encoding="utf-8")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertEqual(l_.code, 0)
        self.assertIn("sachverhalt", {r for r, _ in l_.runner.rollen()})
        self.assertFalse(l_.event("legal.container.reused"))


class GrundlageTest(_FortsetzungBasis):
    """Geänderte Akte: Rollen laufen neu, bauen aber auf ihrem früheren Ergebnis auf (2026-11-17)."""

    def _mit_neuem_dokument(self) -> tuple[Path, Path, "object"]:
        root, alt = self._erstlauf("gl")
        # Sortiert VOR 01_mail.txt: aus dok_001 wird dok_002, neu ist dok_001.
        (root / "00_nachgereicht.txt").write_text("Kaufvertrag <NAME_1>, Kürzungsklausel.", encoding="utf-8")
        eingaben: dict[tuple[str, str], dict[str, str]] = {}
        prompts_: dict[tuple[str, str], str] = {}

        def merke(rolle: str):
            def f(a, std):
                eingaben.setdefault((rolle, a.familie), {p.name: p.read_text(encoding="utf-8")
                                                         for p in a.in_dir.iterdir()})
                prompts_.setdefault((rolle, a.familie), a.prompt)
                return std
            return f
        ov = {r: merke(r) for r in ("sachverhalt", "rechtsfragen", "researcher", "registercheck", "anwalt")}
        l_ = self.lauf(root=root, argv=["--fortsetzen"], overrides=ov)
        return alt, l_, (eingaben, prompts_)

    def test_neue_unterlage_mit_grundlage_und_hinweis(self) -> None:
        alt, l_, (eingaben, prompts_) = self._mit_neuem_dokument()
        self.assertEqual(l_.code, 0, l_.datei("entscheidungen.md"))
        sv = eingaben[("sachverhalt", "claude")]
        self.assertIn("vorher_sachverhalt.json", sv)
        # Frühere Verweise auf dok_001 (01_mail.txt) zeigen jetzt auf dok_002.
        self.assertIn("dok_002", sv["vorher_sachverhalt.json"])
        self.assertNotIn("dok_001", sv["vorher_sachverhalt.json"])
        p = prompts_[("sachverhalt", "claude")]
        self.assertIn("FORTSETZUNG", p)
        self.assertIn("Neu oder geändert gegenüber dem früheren Lauf (Akte): dok_001.", p)
        self.assertIn("umgestellt", p)
        self.assertTrue(l_.event("legal.resume.basis"))
        container = [e for e in l_.jsonl() if e.get("typ") == "container" and e.get("rolle") == "sachverhalt"]
        self.assertTrue(all(e.get("grundlage_aus") == alt.name for e in container))
        # Protokollierter Schlüssel ohne Grundlage-Datei (sonst wäre ein späteres Fortsetzen unmöglich).
        self.assertTrue(all("vorher_sachverhalt.json" not in e["eingaben_sha256"] for e in container))
        self.assertIn("Neu oder geändert: dok_001", l_.datei("protokoll.md"))
        self.assertIn("dok_001→dok_002", l_.datei("protokoll.md"))

    def test_registercheck_bleibt_blind(self) -> None:
        _, l_, (eingaben, prompts_) = self._mit_neuem_dokument()
        rc = eingaben.get(("registercheck", "codex"))
        if rc is not None:   # läuft nur, wenn nicht exakt wiederverwendet
            self.assertEqual(set(rc), {"register_eintraege.json"})
            self.assertNotIn("FORTSETZUNG", prompts_[("registercheck", "codex")])

    def test_unveraendert_bleibt_reine_uebernahme(self) -> None:
        root, _ = self._erstlauf("u")
        l_ = self.lauf(root=root, argv=["--fortsetzen"])
        self.assertEqual(l_.runner.aufrufe, [])
        self.assertFalse(l_.event("legal.resume.basis"))

    def test_manipulierte_grundlage_wird_nicht_verwendet(self) -> None:
        root, alt = self._erstlauf("m")
        datei = next(alt.glob("*_sachverhalt_claude/sachverhalt.json"))
        datei.write_text(datei.read_text(encoding="utf-8").replace("Mietvertrag", "INJIZIERT"), encoding="utf-8")
        (root / "00_neu.txt").write_text("Neue Unterlage.", encoding="utf-8")
        gesehen: list[set[str]] = []

        def sv(a, std):
            if a.familie == "claude":
                gesehen.append({p.name for p in a.in_dir.iterdir()})
            return std
        l_ = self.lauf(root=root, argv=["--fortsetzen"], overrides={"sachverhalt": sv})
        self.assertTrue(gesehen)
        self.assertNotIn("vorher_sachverhalt.json", gesehen[0])
        self.assertTrue(l_.event("legal.resume.mismatch"))

    def test_researcher_grundlage_unterliegt_minimierung(self) -> None:
        root, alt = self._erstlauf("r")
        datei = next(alt.glob("*_researcher_claude/register.json"))
        daten = json.loads(datei.read_text(encoding="utf-8"))
        daten["eintraege"][0]["leitsatz"] = "Kontakt max@example.org"
        neu = json.dumps(daten).encode()
        alt_hash = hashlib.sha256(datei.read_bytes()).hexdigest()
        datei.write_bytes(neu)
        prot = alt / "protokoll.jsonl"
        prot.write_text(prot.read_text(encoding="utf-8").replace(alt_hash, hashlib.sha256(neu).hexdigest()),
                        encoding="utf-8")
        gesehen: list[set[str]] = []

        def researcher(a, std):
            gesehen.append({p.name for p in a.in_dir.iterdir()})
            return std
        # Neue Unterlage + geänderte Rechtsfragen erzwingen einen neuen Researcher-Lauf.
        (root / "00_neu.txt").write_text("Neue Unterlage.", encoding="utf-8")
        ov = {"sachverhalt": lambda a, s: {**s, "luecken": ["neu"]},
              "rechtsfragen": lambda a, s: {"fragen": ["Andere abstrakte Frage"], "normen": ["§ 433 BGB"]},
              "researcher": researcher}
        l_ = self.lauf(root=root, argv=["--fortsetzen"], overrides=ov)
        self.assertEqual(gesehen, [{"rechtsfragen.md"}])
        self.assertTrue(l_.event("legal.resume.basis_rejected"))
