---
id: SPEC-ki-rechtsberatung
companions:
  - agent-roles.md
  - ablauf-diagramm.md
  - brownfield.md
sources: []
---

Type: specification
Status: proposed
Approved-by:
Approved-at:
Applies-to:
- `rechtsberatung/**`

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# KI-Rechtsanwalt-Beratung (`legal`)

## Why

Vision und Schmerz zugleich: Timo will zu eigenen Rechtsfällen (Akte aus E-Mails und Dokumenten) ein belastbares Ergebnis, das Gegenargumente und Richtersicht bereits eingepreist hat, ohne jeden Fall manuell mit einer KI durchzuarbeiten. Ein einzelnes Modell bestätigt sich selbst, erfindet Aktenzeichen und schreibt zu lang. Mehrere Rollen auf zwei Modellfamilien (Claude, Codex), die sich gegenseitig prüfen, sollen das verhindern. Das Muster ist in `immobilienbewertung` bereits erprobt.

## Capabilities

- **CAP-1**
  - **intent:** Das Tool nimmt `anweisung.md` und die Akte aus `fall/` auf, pseudonymisiert sie und erstellt Sachverhalt, Chronologie und Lückenliste mit Quellenverweis je Tatsache.
  - **success:** Jede Tatsache im Sachverhalt verweist auf eine Datei in `fall/`; im Container-Mount findet ein Test keinen Klarnamen aus der Akte.
- **CAP-2**
  - **intent:** Ein Rechtsprechungs-Researcher sucht im Web nach einschlägigen Normen und Urteilen und führt ein Register (Gericht, Az., Datum, Fundstelle, Status geprüft/ungeprüft/hält nicht); er darf das Ergebnis nicht ändern, nur widersprechen.
  - **success:** Jedes Urteil in `beratung.md` hat einen Registereintrag mit Fundstelle; ein frei erfundenes Az. im Entwurf führt zu einem Widerspruch im Protokoll.
- **CAP-3**
  - **intent:** Der Mandanten-Anwalt (Kläger-Anwalt) erstellt das Ergebnis mit Hauptgewicht; Claude und Codex entwerfen unabhängig und reviewen sich gegenseitig.
  - **success:** Protokoll zeigt zwei unabhängige Entwürfe, mindestens eine Cross-Review-Runde und die gewählte Fassung samt gewichteter Benotung.
- **CAP-4**
  - **intent:** Gegenanwalt (stärkste Gegenargumente) und Richter (Erfolgsaussicht, Beweislast, Schlüssigkeit) fließen mit geringerem Gewicht in die Benotung ein; jeder Einwand bekommt eine dokumentierte Antwort.
  - **success:** Jeder Einwand im Protokoll hat den Status übernommen/abgelehnt mit Begründung; die Gesamtnote rechnet mit den konfigurierten Gewichtsfaktoren.
- **CAP-5**
  - **intent:** Abschaltbare Personas: Kläger prüft Ziele und Fakten aus Mandantensicht, Beklagter simuliert die Reaktion der Gegenseite.
  - **success:** Mit Schalter aus erscheinen keine Persona-Beiträge; mit Schalter an je ein Beitrag pro Runde im Protokoll.
- **CAP-6**
  - **intent:** Der Größen-Prüfer stellt jeden Abschnitt in Frage, der für den Fall länger ist als nötig, und schlägt Kürzungen vor; nennt `anweisung.md` ein Wortlimit, wird es deterministisch durchgesetzt.
  - **success:** Jede Runde enthält Kürzungsvorschläge mit Wortzahl vorher/nachher; bei gesetztem Limit wird ein Ergebnis darüber per Exit-Code nicht final.
- **CAP-7**
  - **intent:** Ein Zitat- und Fakten-Prüfer liest die Endfassung kalt (das jeweils andere Modell, nur Akte und Register, ohne Entwurfsnotizen) und meldet nicht belegte Aussagen.
  - **success:** Eine eingeschleuste, nicht belegte Tatsache in einem Testentwurf wird gemeldet.
- **CAP-8**
  - **intent:** Ein Fristen- und Risiko-Prüfer benennt Fristen, Verjährung, Zuständigkeit, Formerfordernisse und Kostenrisiko.
  - **success:** Das Ergebnis enthält eine Fristen-Tabelle mit Datum und Quelle oder den ausdrücklichen Vermerk „keine Frist erkennbar“.
- **CAP-9**
  - **intent:** Ein Conductor orchestriert deterministisch (Runden, Abbruchkriterien, Budget, Hold-Datei), routet Rückfragen und erstellt die Liste „nur Timo entscheidet“.
  - **success:** Eine angelegte Hold-Datei stoppt den Lauf vor der nächsten Runde; offene Rückfragen stehen in `entscheidungen.md`.
- **CAP-10**
  - **intent:** Das Tool liefert, was `anweisung.md` verlangt (z. B. Einschätzung, Schreiben, Schriftsatz), als `beratung.md` plus nachvollziehbares Protokoll aller Agentenbeiträge, Einwände und Antworten.
  - **success:** Zwei Testfälle mit unterschiedlichem Auftrag in `anweisung.md` ergeben unterschiedliche Ergebnisformen; für jede Aussage lässt sich aus dem Protokoll Rolle und Modell ermitteln.
- **CAP-11**
  - **intent:** Ein Vergleichs- und Verhandlungsstratege leitet aus Richter- und Beklagten-Sicht eine Vergleichsspanne ab, nur wenn `anweisung.md` das verlangt.
  - **success:** Ohne entsprechenden Auftrag in `anweisung.md` läuft der Stratege nicht; mit Auftrag enthält das Ergebnis eine begründete Spanne.

## Acceptance criteria

- Die `success`-Kriterien von CAP-1 bis CAP-11 sind durch Tests oder einen dokumentierten Demonstrationslauf belegt.
- Negative Tests belegen: Lauf bricht ohne Pseudonymisierung ab; Anweisungen in `fall/`-Dokumenten und Webseiten werden nicht befolgt; Prüfer können `beratung.md` nicht schreiben; Ergebnis über gesetztem Wortlimit wird nicht final; Urteil ohne Registereintrag wird beanstandet; Nicht-Text-Dateien in `fall/` werden abgelehnt.

## Constraints

- Pseudonymisierung fail-closed vor jedem Modellaufruf; Klartext und Mapping liegen nie im Container-Mount.
- Nur `anweisung.md` und die Tool-Prompts steuern; Inhalte aus `fall/` und aus dem Web sind Daten, keine Anweisungen.
- `fall/` enthält nur `.txt` und `.md`; es gibt keine Konvertierung von E-Mail-, PDF- oder Bildformaten.
- Der Researcher braucht Web-Zugriff; Rollen ohne Rechercheauftrag laufen ohne Netz, soweit die CLI das zulässt.
- Kein Urteil und kein Az. im Ergebnis ohne Registereintrag; ungeprüfte Fundstellen sind sichtbar markiert.
- Prüfer-Rollen haben keinen Schreibzugriff auf das Ergebnis, durchgesetzt per Mount bzw. Tool-Liste, nicht per Prompt.
- Jeder Modellaufruf läuft in einem kurzlebigen, gehärteten Container; Claude-Default `claude-fable-5-2`, Modell je Rolle per Umgebungsvariable überschreibbar.
- Das Gewicht ist ein Faktor in der Benotung: Mandanten-Anwalt 1.0, Richter 0.5, Gegenanwalt 0.5; konfigurierbar.
- Das Wortlimit ist nicht fest; es kommt aus `anweisung.md` oder entfällt.
- Das Ergebnis ist ein KI-Entwurf zur Entscheidung durch Timo; nichts wird automatisch versendet oder eingereicht.

## Non-goals

- Kein Versand von E-Mails, keine Einreichung bei Gericht, keine beA-Anbindung.
- Keine Rechtsdienstleistung für Dritte; das Tool dient Timos eigenen Fällen.
- Keine Web-Oberfläche; nur CLI.
- Keine Konvertierung oder Texterkennung für die Akte.

## Success signal

- `legal.bash <fallordner>` liefert für einen realen Fall das in `anweisung.md` verlangte Ergebnis, dessen Urteile alle mit Fundstelle im Register stehen, mit Gegenargumenten, Richtereinschätzung, Fristen und einer Entscheidungsliste für Timo, ohne Klarnamen im Container.

## Assumptions

- Python (`legal.py`) mit Bash-Wrapper `legal.bash`, damit Docker-Runner, Pseudonymisierung, Logging und Review-Logik aus `immobilienbewertung` wiederverwendet werden können.
- Aufruf `legal.bash <fallordner>` mit `<fallordner>/anweisung.md` und `<fallordner>/fall/`; Code unter `rechtsberatung/`.
- Der Mandant kann Kläger oder Beklagter sein; die Seite steht in `anweisung.md`, der Kläger-Anwalt heißt intern Mandanten-Anwalt.
- Die Modell-ID für Fable 5.2 lautet `claude-fable-5-2`; der Harness wird von Timo aktualisiert.
- „anweisung.txt“ in Timos Antwort ist ein Tippfehler für `anweisung.md`.
