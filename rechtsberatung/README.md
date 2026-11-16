# `legal` – KI-Rechtsanwalt-Beratung (Entwurf für Timo)

`legal.bash <fallordner>` erstellt zu einem eigenen Rechtsfall das in `anweisung.md`
verlangte Ergebnis. Mehrere Rollen auf zwei Modellfamilien (Claude Code, Codex) prüfen
sich gegenseitig. Ein deterministischer Conductor (Host-Python, kein LLM) steuert den
Ablauf, prüft jede Container-Ausgabe und schreibt als einziger die Ergebnisdateien.

**Das Ergebnis ist ein KI-Entwurf, keine Rechtsberatung. Prüfung, Entscheidung und
Versand erfolgen durch Timo.** `legal` versendet und reicht nichts ein.

Maßgeblich: `docs/specs/spec-ki-rechtsberatung/SPEC.md`, Threat Model
`docs/threat-models/ki-rechtsberatung.md` (TM-AC-1–26, E-1–E-13), Plan
`docs/implementation-artifacts/spec-ki-rechtsberatung-legal.md`.

## Aufruf

```bash
rechtsberatung/legal.bash /pfad/zum/fallordner [--keep]
```

Fallordner:

```
<fallordner>/
  anweisung.md      # Auftrag (≤ 64 KiB, UTF-8)
  fall/             # vorab pseudonymisierte Akte: nur .txt/.md, keine Symlinks/Hardlinks/
                    # Unterordner/versteckten Dateien; je Datei ≤ 2 MiB, ≤ 200 Dateien, ≤ 20 MiB
  HOLD              # optional: stoppt den Lauf vor der nächsten Runde/Rolle (Exit 4)
  ergebnis/<lauf-id>/   # wird von legal angelegt
```

`legal` pseudonymisiert nicht und prüft die Pseudonymisierung nicht nach; das geschieht
vorher. Ein Mapping darf nie in `fall/` liegen (`mapping.md` bzw. `LEGAL_MAPPING_NAMEN`
führt zum Abbruch). Keine Konvertierung von PDF, E-Mail oder Bildern.

### Schlüsselzeilen in `anweisung.md` (E-7)

Nur diese Zeilen steuern den Lauf; gleichlautende Zeilen in `fall/` oder in
Modellausgaben haben keine Wirkung. Markdown-Schreibweisen wie `- Seite: Kläger` oder
`**Vergleich:** ja` werden erkannt.

| Zeile | Werte | Default |
|---|---|---|
| `Ergebnis: <Text>` | ≤ 80 Zeichen, z. B. `Einschätzung`, `Schreiben an Vermieter`, `Schriftsatz` | `Einschätzung` |
| `Seite: Kläger\|Beklagter` | Mandantenseite | fehlt → Rückfrage in `entscheidungen.md` |
| `Wortlimit: <n>` | 50–20000; gilt für den Hauptteil (Modelltext) | kein Limit |
| `Personas: an\|aus` | Kläger-/Beklagten-Persona | `aus` |
| `Vergleich: ja\|nein` | Vergleichsstratege (`vergleich_intern.md`) | `nein` |

Ungültige oder doppelte Schlüsselzeilen → Exit 2.

## Ausgaben (`<fallordner>/ergebnis/<lauf-id>/`, Dateien 0600, Verzeichnis 0700)

| Datei | Inhalt |
|---|---|
| `beratung.md` | Kopfzeile „KI-Entwurf …“, `# <Ergebnis-Art>`, Hauptteil, `## Anhang (intern)` mit Fristen-Tabelle (oder „keine Frist erkennbar“) und ungeprüften Fundstellen; bei nicht finalem Lauf zusätzlich „NICHT FINAL“ |
| `entscheidungen.md` | Früheste Frist zuoberst, Frist-Abweichungen, Status/Gates, Rückfragen, Lücken, offene/abgelehnte Einwände, ungeprüfte Fundstellen, Widersprüche, Injektions-Hinweise, feste Punkte (E-5, E-6). Nur Timo hakt ab. |
| `register.md` | Rechtsprechungs-Register; Status `geprüft\|ungeprüft\|hält nicht` setzt nur der Host |
| `protokoll.md` / `protokoll.jsonl` | Append-only: alle Beiträge, Einwände, Antworten, Noten, Gewichte, gewählte Fassung; je Container Lauf-ID, Runde, Rolle, Familie, Modell, Start/Ende, rc, SHA-256 von Ein- und Ausgabe; Zuordnung `dok_nnn ↔ Datei` |
| `vergleich_intern.md` | nur bei `Vergleich: ja`; Kopf „intern – nicht an Gegenseite“ |

Arbeitsverzeichnisse (`tempfile`, 0700) werden nach dem Lauf gelöscht; `--keep` behält
sie (E-9). Ergebnisse bleiben, bis Timo sie löscht.

## Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | final |
| 1 | unerwarteter Fehler (ohne Stacktrace) |
| 2 | Eingabe oder Konfiguration ungültig (vor jedem Modellaufruf), Mount-Gate |
| 3 | nicht final (Rolle fehlgeschlagen, Zitat-/Wortlimit-/Pflichtabschnitt-/Einwand-/Strategie-Gate) |
| 4 | Hold-Datei, Aufrufbudget, Gesamtlaufzeit oder Signal |

## Ablauf

Sachverhalt ×2 (Abgleich, Quellen-Gate) → Rechtsfragen (Claude; Minimierungsprüfung,
sonst kein Researcher) → Researcher (Claude, Recherche-Netz) → Register-Bestätiger
(Codex, Blindbestätigung: erhält nur ID und URL und liest Gericht/Az./Datum selbst aus) → Mandanten-Anwalt ×2 → Cross-Review (`LEGAL_REVIEW_RUNDEN`)
→ Richter + Gegenanwalt bewerten beide Entwürfe (jeweils andere Familie) → gewichtete
Gesamtnote, Gleichstand → Claude → Fristen ×2 → Stratege (nur `Vergleich: ja`) →
Schleife bis `LEGAL_MAX_RUNDEN`: Personas (nur `Personas: an`) → Überarbeitung → Größen-Prüfer
→ Cold-Read (nur Endfassung, Akte, Register) → Abschluss-Gates.

Rollen-Ausgaben sind JSON-Dateien in `/out` (Schemata in `rueckkanal.py`); der Host liest
nur erwartete Namen, ohne Symlinks/Hardlinks, ≤ 1 MiB.

## Konfiguration (Umgebungsvariablen)

| Variable | Default |
|---|---|
| `LEGAL_CLAUDE_MODEL_<ROLLE>` → `LEGAL_CLAUDE_MODEL` | `claude-fable-5-2` |
| `LEGAL_CODEX_MODEL_<ROLLE>` → `LEGAL_CODEX_MODEL` | leer = CLI-Default (`codex-default` im Protokoll) |
| `LEGAL_GEWICHT_ANWALT` / `_RICHTER` / `_GEGENANWALT` | 1.0 / 0.5 / 0.5 (0 < w ≤ 1) |
| `LEGAL_MAX_RUNDEN`, `LEGAL_MAX_AUFRUFE`, `LEGAL_TIMEOUT_S`, `LEGAL_MAX_LAUFZEIT_S` | 3, 60, 1800, 14400 |
| `LEGAL_REVIEW_RUNDEN` | 1 |
| `LEGAL_IMAGE` | `legal:1` |
| `LEGAL_LLM_DOMAINS` (ersetzt) | `api.anthropic.com, api.openai.com, chatgpt.com, auth.openai.com` |
| `LEGAL_RESEARCH_DOMAINS` (ergänzt E-4) | Liste aus E-4 |
| `LEGAL_PSEUDONYM_REGEX` | `<[A-ZÄÖÜ_]+_?\d+>\|\b[A-ZÄÖÜ]{3,}_\d+\b` |
| `LEGAL_MAPPING_NAMEN` | zusätzlich zu `mapping.md` |
| `LEGAL_LOG_LEVEL` | `INFO` |

Rollen: `sachverhalt, rechtsfragen, researcher, registercheck, anwalt, review, ueberarbeitung,
richter, gegenanwalt, persona_klaeger, persona_beklagter, fristen, stratege, groesse, coldread`.
Modell-IDs müssen `^[a-z0-9][a-z0-9.\-]{1,63}$` erfüllen; jeder Override loggt
`[SEC] event=legal.model.override`. Ungültige Werte → Exit 2.

## Image bauen

```bash
docker build -t legal:1 rechtsberatung/docker
```

Basis per Digest, CLIs per `npm ci` aus `package-lock.json` (Version und Integrity),
setuid/setgid-Bits entfernt, kein ssh/sudo, `USER node` (UID 1000). Der Egress-Proxy
(`egress-proxy.js`, nur Node-Standardbibliothek) läuft aus demselben Image.

Laufzeit: `LEGAL_IMAGE` wird zu Laufbeginn per `docker image inspect` auf die unveränderliche
Image-ID aufgelöst; alle Container laufen über diese ID, sie steht in `protokoll.md` und
`protokoll.jsonl`. Je Lauf entstehen zwei `docker network create --internal`-Netze (`api`,
`recherche`) mit je einem Proxy-Container; die Proxys hängen zusätzlich an einem eigenen,
nicht internen Egress-Netz des Laufs (nicht am Default-Bridge-Netz). Rollen-Container starten
erst, wenn beide Proxys auf Port 3128 annehmen. Der Proxy erlaubt nur `CONNECT`
auf Port 443 zu den freigegebenen Domains (inkl. Subdomains), sperrt private Zieladressen
und loggt Ablehnungen als `[SEC] event=legal.egress.denied role=<r> domain=<d>`.

## Credentials (E-3)

Empfohlen: eigene API-Keys mit DPA, ohne Training und mit Ausgabelimit, als
`ANTHROPIC_API_KEY` bzw. `OPENAI_API_KEY` im Host-Env. Sie gehen nur per Name (`-e NAME`)
in den Container, nie als Wert ins argv. Fehlt der Key, wird `~/.claude/.credentials.json`
bzw. `~/.codex/auth.json` (0600) in ein temporäres Home je Container kopiert und danach
gelöscht. **Risiko:** Bei OAuth-Anmeldungen kann die CLI im Container das Token
erneuern; die erneuerte Datei wird verworfen, das Host-Token kann dadurch ungültig
werden. Host-`~/.claude`, `~/.claude.json`, `~/.codex`, `~/.ssh` und `docker.sock` werden
nie gemountet.

Vor dem ersten Modellaufruf prüft `legal` je Modellfamilie, ob API-Key oder Auth-Datei vorhanden
ist; sonst Exit 2 mit `[SEC] event=legal.credentials.missing outcome=rejected`.

Die Container laufen mit UID/GID des aufrufenden Host-Nutzers (`--user $(id -u):$(id -g)`),
damit die 0700-Arbeitsverzeichnisse lesbar bzw. beschreibbar sind.

## Logs

`[SEC]`-Events auf stderr mit Zeitstempel, Nutzer, Lauf-ID, Komponente und Ergebnis.
Logs enthalten keine Akten-, Prompt- oder Modellinhalte und keine Dateinamen aus `fall/`
(nur Index). Steuerzeichen und ANSI/OSC-Sequenzen werden entfernt.

## Tests

```bash
python3 -m unittest discover -s rechtsberatung/tests -t . -v
node --test rechtsberatung/docker/egress-proxy.test.js
LEGAL_DOCKER_TESTS=1 python3 -m unittest discover -s rechtsberatung/tests/integration -t .   # mit Docker + Image
```

Unit-Tests rufen Docker nie auf (injizierter `FakeRunner`). Die Docker-Integrationstests
(`tests/integration/`: CLI-Pins, setuid, Netzsperre, Read-only-Mount/Host-Konfig-Hash,
`docker ps` nach Timeout) werden ohne `LEGAL_DOCKER_TESTS=1` übersprungen.

## Supply-Chain-Bewertung (supply-chain.md, Stand 2026-11-16)

| Komponente | Version | Ergebnis |
|---|---|---|
| Basis `node:22.23.4-bookworm-slim` | `@sha256:5ec23b7c…3d96d` | Docker Official Image; Tag ≥ 7 Tage alt; läuft als `node` (UID 1000); setuid/setgid-Bits werden im Build entfernt |
| `@anthropic-ai/claude-code` | 2.1.319 (2026-11-06) | OSV: keine Funde (geprüft 2026-11-16); neuere 2.1.325 (2026-11-13) ist < 7 Tage alt; Scorecard: kein Projekt (Repo ohne Quellcode) → erweiterte Prüfung: Publisher Anthropic, offizieller Scope `@anthropic-ai`, bereits im Vorbild genutzt. Hat ein `postinstall` (`install.cjs`) zum Verlinken des Plattform-Binärpakets, daher `npm ci` ohne `--ignore-scripts`. |
| `@openai/codex` | 0.167.0 (2026-11-06) | OSV: keine Funde (geprüft 2026-11-16); neuere 0.169.0 (2026-11-13) < 7 Tage alt; Scorecard `openai/codex` 6 |

Plattform-Binärpakete beider CLIs stehen mit Integrity im Lockfile.

## Offene Betriebspunkte

- **E-3:** API-Konten mit DPA, ohne Training, eigenes Credential mit Ausgabelimit (Timo).
- **E-5:** Art.-9-Inhalte und Vertraulichkeitsabreden vor jedem Lauf prüfen (fester Punkt in `entscheidungen.md`).
- **E-6:** Festplattenverschlüsselung, Fallordner nicht in Cloud-Sync (fester Punkt in `entscheidungen.md`).
- **Malware- und Schwachstellenscan des Images** (supply-chain.md, Must-have 1) ist offen: In der Implementierungsumgebung gab es kein Docker. Vor der ersten Nutzung z. B. mit Trivy/Grype prüfen und den Digest bestätigen.
- **Docker-Integrationstests** (`tests/integration/`) sind nicht gelaufen (kein Docker); vor der ersten Nutzung mit `LEGAL_DOCKER_TESTS=1` ausführen.
- **CLI-Verhalten im Container nicht live verifiziert:** u. a. `claude --add-dir /in` mit `--permission-mode acceptEdits`, Codex-Abruf von URLs über den Proxy (`NODE_USE_ENV_PROXY=1`, kein `curl` im Image) und die Schreibbarkeit des Container-Homes für die CLIs.
- WebSearch von Claude (Researcher) läuft serverseitig beim Anbieter und umgeht den Proxy (R-6, E-2).
