---
title: 'legal: aktuelles Verzeichnis als Fall-Ordner'
type: 'feature'
created: '2026-11-16'
status: 'done'
baseline_revision: '6df9fcdecf3473aaadd5a522956246ae641e1ce9'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/docs/specs/spec-ki-rechtsberatung/SPEC.md'
warnings: []
deferred:
  - summary: >-
      E-6 (kein Cloud-Sync-Ordner) ist nur betrieblich geregelt; mit dem aktuellen Verzeichnis als Default wird ein Start im Sync-Ordner wahrscheinlicher.
    evidence: |-
      Timo hat legal.bash bereits in ~/Nextcloud-privat/... gestartet. Ein technisches Gate (bekannte Sync-Pfade oder Marker) wäre eine Änderung der freigegebenen Entscheidung E-6 und braucht Timos Entscheidung.
    location: >-
      rechtsberatung/eingabe.py:pruefe_fallordner
    severity: medium
  - summary: >-
      Dieser Build-Plan hat keine Freigabe-Metadaten von Timo; die Schreibberechtigung stützt sich auf den freigegebenen Plan spec-ki-rechtsberatung-legal.md (Applies-to rechtsberatung/**).
    evidence: |-
      AGENTS.md verlangt freigegebene Plan-Artefakte; der Agent darf eigene Artefakte nicht freigeben. Menschliches Review von Plan, Code und Tests steht aus.
    location: >-
      docs/implementation-artifacts/spec-legal-fallordner-cwd.md
    severity: medium
---

<intent-contract>

## Intent

**Problem:** `legal.bash` verlangt den Fall-Ordner als Pflichtargument. Timo startet immer im Fall-Ordner, und ohne Argument bricht argparse ab („the following arguments are required: fallordner“).

**Approach:** Das Argument `fallordner` wird optional. Fehlt es, gilt das Verzeichnis des Aufrufers als Fall-Ordner (SPEC-Constraint vom 2026-11-16): `LEGAL_AUFRUF_CWD` vom Wrapper, sonst `os.getcwd()`. Ein übergebener Pfad funktioniert unverändert.

## Boundaries & Constraints

**Always:** Alle Eingabe-Gates (TM-AC-1, TM-AC-4) gelten für den Fall-Ordner aus dem aktuellen Verzeichnis genauso wie für einen übergebenen Pfad. Abbruch vor jedem Modellaufruf, Exit 2. Logs und Fortschrittsmeldungen enthalten keine Pfade.

**Never:** Kein Hochsuchen in Elternverzeichnissen nach `anweisung.md`. Keine Änderung an Ausgabeort (`<fall-ordner>/ergebnis/<lauf-id>/`), Hold-Datei oder Gates. Das Pfad-Argument wird nicht entfernt.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| CWD_OK | `legal.bash` ohne Argument im gültigen Fall-Ordner | Lauf wie mit `legal.bash .`; Ergebnis unter `./ergebnis/<lauf-id>/` | keins |
| CWD_FALSCH | ohne Argument in einem Ordner ohne `anweisung.md` | Exit 2, kein Modellaufruf, kein `ergebnis/` | `[SEC] legal.input.rejected reason=missing_anweisung`; Meldung nennt den Grund und dass das aktuelle Verzeichnis als Fall-Ordner gilt |
| ARG_RELATIV | `legal.bash fall1` aus dem Elternordner | unverändert: Auflösung gegen `LEGAL_AUFRUF_CWD` | wie bisher |
| OHNE_WRAPPER | `python3 -m rechtsberatung.legal` ohne Argument, ohne `LEGAL_AUFRUF_CWD` | `os.getcwd()` ist der Fall-Ordner | wie CWD_FALSCH |

</intent-contract>

## Code Map

- `rechtsberatung/legal.py:33-37` -- `_parser()`: Positionsargument `fallordner` ist Pflicht; wird `nargs="?"`, Default `None`, Hilfetext nennt das aktuelle Verzeichnis.
- `rechtsberatung/legal.py:~66-70` -- `main()`: `pfad = Path(args.fallordner)` + Auflösung gegen `LEGAL_AUFRUF_CWD`; hier den Default ergänzen.
- `rechtsberatung/legal.py:~91-93` -- `except (EingabeFehler, MountUnsicher)`: `progress(... + hinweis(...))`; ohne Argument den Zusatz „Fall-Ordner ist das aktuelle Verzeichnis“ anhängen, ohne Pfad.
- `rechtsberatung/legal.bash` -- exportiert `LEGAL_AUFRUF_CWD="$PWD"` und wechselt ins Repo; Kommentarzeile `Aufruf:` anpassen. Die Logik bleibt.
- `rechtsberatung/tests/test_eingabe.py` -- `Basis.lauf_abgewiesen()` und `erstelle_fall()`; dort die Tests für CWD_OK, CWD_FALSCH und OHNE_WRAPPER.
- `rechtsberatung/tests/fake.py` -- `FakeRunner` für Läufe ohne Docker.
- `rechtsberatung/README.md:3,18` -- Aufrufbeispiele.
- `docs/threat-models/ki-rechtsberatung.md` -- Abschnitt „Änderungen nach Freigabe“; kurzer Eintrag, keine neue Bedrohung.

## Tasks & Acceptance

**Execution:**
- `rechtsberatung/legal.py` -- Argument optional, Default-Auflösung `LEGAL_AUFRUF_CWD` → `os.getcwd()`, Fehlermeldungszusatz ohne Pfad -- SPEC-Constraint.
- `rechtsberatung/legal.bash` -- Kommentar `Aufruf: legal.bash [fallordner] [--keep]` -- Doku im Wrapper.
- `rechtsberatung/tests/test_eingabe.py` -- Tests CWD_OK (Exit 0 bzw. Ergebnisordner im Fall-Ordner mit FakeRunner), CWD_FALSCH (Exit 2, `reason=missing_anweisung`, kein Runner-Aufruf, Zusatzhinweis in der Meldung), OHNE_WRAPPER (`os.chdir` in einen Fall-Ordner, ohne `LEGAL_AUFRUF_CWD`) -- I/O-Matrix.
- `rechtsberatung/README.md` -- Aufruf `cd <fall-ordner> && legal.bash`, Pfad optional -- Nutzerdoku.
- `docs/threat-models/ki-rechtsberatung.md` -- Zeile in „Änderungen nach Freigabe“: Fall-Ordner = aktuelles Verzeichnis, Gates unverändert, Hinweis E-6 (kein Cloud-Sync-Ordner) -- Nachvollziehbarkeit.

**Acceptance Criteria:**
- Given ein gültiger Fall-Ordner als aktuelles Verzeichnis, when `legal.bash` ohne Argument läuft, then entsteht `ergebnis/<lauf-id>/` in genau diesem Ordner.
- Given `legal.bash --help`, when die Hilfe erscheint, then ist `fallordner` als optional markiert und der Hilfetext nennt das aktuelle Verzeichnis als Default.

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 23 findings — high 0, medium 8, low 14, false 1, maybe-false 0
- findings:
  - `[medium]` `[patch]` IA: Erwartung liegt an der Shell (`legal.bash`), Tests prüfen nur `main()` — WrapperTest mit echtem Wrapper-Subprocess ergänzt (gültiger Ordner → credentials.missing, leerer Ordner → missing_anweisung, Symlink).
  - `[medium]` `[patch]` IA: Aufruf aus dem Fall-Ordner braucht weiter den vollen Wrapper-Pfad; Symlink auf PATH bricht — `readlink -f "${BASH_SOURCE[0]}"` im Wrapper, README zeigt Symlink/Alias.
  - `[false]` `[reject]` IA: Lesart „Argument entfernen“ (R2) weicht ab — Timos Satz „muss nicht immer extra angegeben werden“ lässt eine gelegentliche Angabe ausdrücklich zu; SPEC-Assumption dokumentiert das.
  - `[low]` `[patch]` IA: Zitat in TM/SPEC ohne zweites „immer“ — wörtlich korrigiert (Memlog append-only unverändert).
  - `[medium]` `[patch]` VG: Übergabe `LEGAL_AUFRUF_CWD` durch den Wrapper ungetestet — gleiche Gruppe wie IA-1; Mutation (Export entfernt) lässt die neuen Wrapper-Tests (a)/(c) fehlschlagen.
  - `[low]` `[reject]` EC: veraltetes exportiertes `LEGAL_AUFRUF_CWD` beim Direktstart von Python — nur ohne Wrapper erreichbar, der Wrapper setzt die Variable immer neu; Guard bräuchte neuen Marker.
  - `[low]` `[reject]` EC: relatives `LEGAL_AUFRUF_CWD` — Wrapper setzt immer `$PWD` (absolut); Fall im Alltag nicht erreichbar, Fix wäre zusätzliche Validierung.
  - `[low]` `[patch]` EC: `OSError`-Zweig ohne CWD-Hinweis — `zusatz` auch dort angehängt, Test mit `os.getcwd` → FileNotFoundError.
  - `[low]` `[reject]` EC: `python -m` aus dem Fall-Ordner legt cwd auf `sys.path` — Wrapper wechselt ins Repo; Direktstart ist nicht dokumentiert, Angriff braucht Schreibzugriff auf Timos Fall-Ordner; Verhalten bestand schon vor der Änderung.
  - `[medium]` `[patch]` EC: Wrapper über PATH-Symlink — gleiche Gruppe wie IA-2.
  - `[low]` `[reject]` EC: leeres Argument `""` nutzt still das Aufrufverzeichnis ohne Hinweis — Ergebnis ist korrekt, nur der Hinweis fehlt; kosmetisch, Fix bräuchte Sonderfall.
  - `[low]` `[patch]` EC: Tests für veraltetes/relatives Env und OSError fehlen — OSError-Test ergänzt; Env-Fälle mit den abgelehnten Befunden abgelehnt.
  - `[medium]` `[patch]` BH: Wrapper über PATH-Symlink, Installation undokumentiert — gleiche Gruppe wie IA-2.
  - `[low]` `[patch]` BH: TM „Keine neue Bedrohung“ unbegründet — als Variante von T-24 mit Begrenzung beschrieben.
  - `[medium]` `[defer]` BH: E-6 nur betrieblich, CWD-Default macht Sync-Ordner wahrscheinlicher — Änderung einer freigegebenen TM-Entscheidung, braucht Timo → deferred.
  - `[medium]` `[defer]` BH: Build-Plan ohne Freigabe-Metadaten; TM-User-Story und Plan Schritt 11 veraltet — User-Story korrigiert, Change-Log-Eintrag im Plan; Freigabe durch Timo nicht durch den Agenten möglich → deferred.
  - `[low]` `[patch]` BH: keine Abuse Story zum geänderten Aufruf — CAP-9-Abuse-Story um untergeschobene `anweisung.md`/`fall/` ergänzt (T-24).
  - `[low]` `[reject]` BH: ACs decken nur Erfolgsfall und Hilfe ab, Matrix ohne Gate-Zeilen — Fix wäre Änderung dieses Build-Specs.
  - `[low]` `[reject]` BH: `LEGAL_AUFRUF_CWD` nicht validiert — siehe EC (Wrapper setzt immer absolutes `$PWD`).
  - `[medium]` `[patch]` BH: Wrapper selbst ungetestet — gleiche Gruppe wie VG.
  - `[low]` `[patch]` BH: OSError-Zweig ohne Hinweis; `[SEC]` ohne `quelle=cwd|argument` — Hinweis ergänzt (siehe EC); zusätzliches Log-Feld abgelehnt (kein Nutzen ohne Pfad).
  - `[low]` `[reject]` BH: schwache Tests (kein subTest, fragiles `parents[1]`, Cleanup nach Erfolg) — nur Entwicklerkomfort, Umbau mehr als direkte Korrektur.
  - `[low]` `[patch]` BH: README uneinheitlich/überlange Zeile — Absatz neu umbrochen, Schreibweise „Fallordner“ vereinheitlicht.

## Verification

**Commands:**
- `cd /workspace && python3 -m unittest discover -s rechtsberatung/tests -t .` -- expected: OK, nur Docker-Tests übersprungen.
- `bash -n rechtsberatung/legal.bash` -- expected: kein Fehler.
- `bandit -q -r rechtsberatung -x rechtsberatung/tests` -- expected: keine Befunde.

## Auto Run Result

**Zusammenfassung:** `legal.bash` nimmt ohne Argument das Verzeichnis des Aufrufers als Fall-Ordner (`LEGAL_AUFRUF_CWD` vom Wrapper, sonst `os.getcwd()`); ein Pfad bleibt optional. Kein Hochsuchen, alle Gates unverändert, Fehlermeldungen nennen den Grund und den CWD-Bezug ohne Pfad. Der Wrapper funktioniert jetzt auch über Symlinks.

**Geänderte Dateien:**
- `rechtsberatung/legal.py` -- `fallordner` optional, `_fallordner()`, CWD-Hinweis in Eingabe- und OSError-Fehlern.
- `rechtsberatung/legal.bash` -- Symlink-Auflösung per `readlink -f`, Kommentar.
- `rechtsberatung/tests/test_eingabe.py` -- `AktuellesVerzeichnisTest` (10 Tests), `WrapperTest` (3 Subprocess-Tests mit echtem Wrapper); neue `# nosec B404/B603` für den Test-Subprocess (Testcode, menschliches Review nötig).
- `rechtsberatung/README.md` -- Aufruf im Fallordner, Symlink/Alias, Absatz neu umbrochen.
- `docs/threat-models/ki-rechtsberatung.md` -- Änderung nach Freigabe (T-24-Variante), Abuse Story CAP-9, User Story.
- `docs/specs/spec-ki-rechtsberatung/SPEC.md` -- Zitat wörtlich korrigiert.
- `docs/implementation-artifacts/spec-ki-rechtsberatung-legal.md` -- Spec-Change-Log-Eintrag.

**Review:** 23 Befunde (medium 8, low 14, false 1). 13 Befund-Zeilen als Patch umgesetzt (Gruppen: Wrapper-Test, Symlink, OSError-Hinweis, TM-Doku/Abuse Story, Zitat, README); Patch-Zählung nach Entry-Verdict: medium 2 (Wrapper-Test, Symlink), low 4. 2 deferred (E-6-Gate, fehlende Freigabe). Abgelehnt: R2-Lesart (false), veraltetes/relatives Env (low, nicht erreichbar via Wrapper), `python -m`-sys.path (low, vorbestehend, nicht dokumentiert), leeres Argument (low, kosmetisch), AC-/Matrix-Lücken (Fix = Build-Spec ändern), schwache Teststruktur (low, Umbau), `[SEC]`-Feld `quelle` (kein Nutzen).

**Follow-up-Review:** `true` -- zwei medium-Einträge gepatcht. Nicht unabhängig verifiziert: der Wrapper-Symlink-Pfad und die Wrapper-Tests laufen nur in dieser Umgebung (Linux, GNU `readlink -f`); ein echter Lauf mit Docker und Credentials steht weiter aus.

**Verifikation:** `python3 -m unittest discover -s rechtsberatung/tests -t .` → 156 OK (7 Docker-Tests übersprungen); `bash -n rechtsberatung/legal.bash` OK; `bandit` 0 Befunde; `semgrep p/python` 0; `gitleaks` (rechtsberatung, docs) keine Leaks; `node --test egress-proxy.test.js` 11/11. Mutation: ohne `export LEGAL_AUFRUF_CWD` bzw. mit altem `$0`-Pfad schlagen die Wrapper-Tests fehl. Security-Drift-Check meldet vorbestehende Abweichungen (AGENTS.md, zwei Hooks, `agent-authority.md`), nicht durch diesen Lauf.

**Restrisiken:** Start in einem Cloud-Sync-Ordner (E-6) wird nicht technisch verhindert; versehentlicher Start in einem fremden Ordner mit eigener `anweisung.md` (T-24-Variante); kein Live-Test mit Docker.
