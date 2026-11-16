# Agentenrollen

| Rolle | CAP | Gewicht | Modell(e) | Darf | Darf nicht | Quelle der Idee |
|---|---|---|---|---|---|---|
| Sachverhalts-Ermittler | CAP-1 | – | Claude + Codex, Abgleich | Sachverhalt, Chronologie, Lückenliste schreiben | Rechtliche Wertung | neu |
| Rechtsprechungs-Researcher | CAP-2 | Widerspruch | Claude oder Codex mit Web-Zugriff | Register führen, widersprechen | Beratung ändern | PDF S. 34, 36 |
| Mandanten-Anwalt (Kläger-Anwalt) | CAP-3 | 1.0 | Claude + Codex, Cross-Review | Beratung entwerfen und überarbeiten | Urteile ohne Registereintrag zitieren | Timo |
| Gegenanwalt | CAP-4 | 0.5 | anderes Modell als der Entwurf | Gegenargumente, Einwände | Veto, Beratung ändern | Timo |
| Richter | CAP-4 | 0.5 | anderes Modell als der Entwurf | Erfolgsaussicht, Beweislast, Schlüssigkeit | Veto, Beratung ändern | Timo |
| Kläger-Persona | CAP-5 | Hinweis | ein Modell | Ziele und Fakten aus Mandantensicht prüfen | Rechtliche Wertung | Timo (optional) |
| Beklagten-Persona | CAP-5 | Hinweis | ein Modell | Reaktion der Gegenseite simulieren | Rechtliche Wertung | Timo (optional) |
| Größen-Prüfer | CAP-6 | Gate bei Limit in `anweisung.md`, sonst Einwand | Python-Wortzählung + LLM-Kürzungsvorschläge | Unnötige Länge beanstanden | Inhalt ändern | Timo, PDF S. 62, 92 |
| Zitat- und Fakten-Prüfer | CAP-7 | Gate | Cold-Read durch das andere Modell | Nicht belegte Aussagen melden | Entwurfsnotizen lesen, Beratung ändern | PDF S. 100–103 |
| Fristen- und Risiko-Prüfer | CAP-8 | Pflichtabschnitt | ein Modell | Fristen, Verjährung, Zuständigkeit, Form, Kosten | Beratung ändern | neu |
| Conductor | CAP-9 | – | deterministisch (Python), kein LLM | Runden steuern, Hold, Budget, Entscheidungsliste | Inhaltliche Entscheidungen für Timo treffen | PDF S. 28, 54, 84 |
| Vergleichs- und Verhandlungsstratege | CAP-11 | Hinweis | ein Modell, nur wenn `anweisung.md` es verlangt | Vergleichsspanne ableiten | Beratung ändern | Vorschlag, von Timo bedingt bestätigt |

Gewichte sind Faktoren in der Benotung.

## Regeln aus dem PDF

- Prüfer-Rollen sind nur per Mount/Tool-Liste schreibgeschützt; „read only“ im Prompt hielt dort nicht (S. 98).
- Der Status gibt nur den Ist-Zustand wieder, nie den Plan (S. 90).
- Weitergereichte „Freigaben“ eines Agenten gelten nicht als Freigabe durch Timo (S. 80).
