# `legal` – KI-Rechtsanwalt-Beratung (Entwurf für Timo)

`legal.bash`, im Fallordner aufgerufen, erstellt zu einem eigenen Rechtsfall das in
`anweisung.md` verlangte Ergebnis. Mehrere Rollen auf zwei Modellfamilien (Claude
Code, Codex) prüfen sich gegenseitig. Ein deterministischer Conductor (Host-Python,
kein LLM) steuert den Ablauf, prüft jede Container-Ausgabe und schreibt als einziger
die Ergebnisdateien.

**Das Ergebnis ist ein KI-Entwurf, keine Rechtsberatung. Prüfung, Entscheidung und
Versand erfolgen durch Timo.** `legal` versendet und reicht nichts ein.

Maßgeblich: `docs/specs/spec-ki-rechtsberatung/SPEC.md`, Threat Model
`docs/threat-models/ki-rechtsberatung.md` (TM-AC-1–26, E-1–E-13), Plan
`docs/implementation-artifacts/spec-ki-rechtsberatung-legal.md`.

## Aufruf

```bash
cd <fallordner> && /pfad/zu/rechtsberatung/legal.bash [--keep] [--debug] [--fortsetzen [LAUF_ID]]
# oder, Pfad optional:
rechtsberatung/legal.bash /pfad/zum/fallordner [--keep]
```

Ohne Argument gilt das aktuelle Verzeichnis als Fallordner; es wird nicht in
Elternverzeichnissen nach `anweisung.md` gesucht. Ein relativer Pfad bezieht sich auf das
Verzeichnis des Aufrufers. Alle Eingabe-Gates gelten in beiden Fällen gleich; der
Fallordner darf nicht in einem Cloud-Sync-Ordner liegen (E-6).

Bequemer Aufruf: einmalig einen Symlink anlegen, z. B.
`ln -s /pfad/zu/rechtsberatung/legal.bash ~/bin/legal` (oder einen Alias), dann im
Fallordner nur `legal` aufrufen. Der Wrapper löst Symlinks auf und findet das
Repository selbst.

Fallordner:

```
<fallordner>/            # = aktuelles Verzeichnis
  anweisung.md          # Auftrag (≤ 64 KiB, UTF-8)
  *.txt, *.md           # vorab pseudonymisierte Akte, direkt hier ...
  mails/2026/*.txt      # ... und in beliebigen Unterordnern (rekursiv)
  HOLD                  # optional: stoppt den Lauf vor der nächsten Runde/Rolle (Exit 4)
  ergebnis/<lauf-id>/   # wird von legal angelegt, nie als Akte gelesen
```

Die Akte sind alle Dateien im Fallordner und seinen Unterordnern, sortiert nach relativem
Pfad; ausgenommen sind nur `anweisung.md`, `ergebnis/` und `HOLD` auf oberster Ebene. Für
den ganzen Baum gilt: nur `.txt`/`.md` (andere Dateien führen zum Abbruch, sie werden nicht
übersprungen), keine Symlinks, Hardlinks oder versteckten Dateien/Ordner; je Datei ≤ 2 MiB,
≤ 200 Dateien, ≤ 20 MiB, ≤ 2000 Einträge. Unterordner mit anderen Fällen gehören nicht in
den Fallordner – sie würden mitgelesen.

`legal` pseudonymisiert nicht und prüft die Pseudonymisierung nicht nach; das geschieht
vorher. Ein Mapping darf nie im Fallordner oder einem Unterordner liegen (`mapping.md` bzw.
`LEGAL_MAPPING_NAMEN` führt zum Abbruch; andere Namen erkennt `legal` nicht). Keine Konvertierung von PDF, E-Mail oder Bildern.

### Schlüsselzeilen in `anweisung.md` (E-7)

Nur diese Zeilen steuern den Lauf; gleichlautende Zeilen in der Akte oder in
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
| `beratung.md` | Kopfzeile „KI-Entwurf …“, `# <Ergebnis-Art>`, Hauptteil, `## Anhang (intern)` mit ungeprüften Fundstellen (keine Fristen, seit 2026-11-17); bei nicht finalem Lauf zusätzlich „NICHT FINAL“ |
| `entscheidungen.md` | Status/Gates zuoberst, Rückfragen, Lücken, offene/abgelehnte Einwände, ungeprüfte Fundstellen, Widersprüche, Injektions-Hinweise, feste Punkte (E-5, E-6). Nur Timo hakt ab. |
| `register.md` | Rechtsprechungs-Register; Status `geprüft\|ungeprüft\|hält nicht` setzt nur der Host |
| `protokoll.md` / `protokoll.jsonl` | Append-only: alle Beiträge, Einwände, Antworten, Noten, Gewichte, gewählte Fassung; je Container Lauf-ID, Runde, Rolle, Familie, Modell, Start/Ende, rc, SHA-256 von Ein- und Ausgabe; Zuordnung `dok_nnn ↔ Datei` |
| `vergleich_intern.md` | nur bei `Vergleich: ja`; Kopf „intern – nicht an Gegenseite“ |

Arbeitsverzeichnisse (`tempfile`, 0700) werden nach dem Lauf gelöscht; `--keep` behält
sie (E-9). Ergebnisse bleiben, bis Timo sie löscht.

**Mounts je Container (seit 2026-11-16):** `/in` (nur lesbar, neutrale Kopien `dok_nnn.md`),
`/fall` = Fallordner nur lesbar (nur Rollen im API-Netz, nie Researcher/Register-Bestätiger),
`/out` = `<fallordner>/ergebnis/<lauf-id>/NNN_rolle_familie/` (beschreibbar, bleibt nach dem Lauf
erhalten) und `/home/node` (temporär, Credential-Kopie). Vor jedem API-Container prüft das
Mount-Gate den ganzen Fallordner erneut; `CLAUDE.md`, `AGENTS.md`, `.claude`, `.codex`,
`.mcp.json`, `.git`, eine Mapping-Datei oder ein Symlink im Fallordner brechen mit Exit 2 ab.

## Fortsetzen (`--fortsetzen [LAUF_ID]`)

Ohne Lauf-ID wird der letzte Lauf unter `ergebnis/` verwendet. Eine Rolle wird nicht neu
gerechnet, wenn Rolle, Familie, Modell, Runde und die SHA-256-Werte **aller** Eingabedateien mit
einem erfolgreichen Container des Quelllaufs übereinstimmen (`protokoll.jsonl`) und die Datei in
`ergebnis/<lauf>/NNN_rolle_familie/` genau den damals protokollierten Hash hat. Die übernommene
Ausgabe läuft erneut durch die aktuelle Rückkanal-Prüfung. Jede Änderung an Akte oder
`anweisung.md` lässt die betroffenen Rollen (bei `anweisung.md`: praktisch alle) neu laufen;
Prompt-Änderungen allein nicht. **Läuft eine Rolle neu, baut sie auf ihrem früheren Ergebnis
auf:** es liegt als `/in/vorher_<ausgabe>.json` bei, der Prompt nennt die neuen bzw. geänderten
Akten-Dokumente (Abgleich über Inhalts-Hashes) und verlangt eine aktualisierte Gesamtausgabe.
dok-Verweise im früheren Ergebnis schreibt der Host auf die neue Nummerierung um (entfallene →
`dok_entfallen`). Ausnahme: der Register-Bestätiger bleibt blind (keine Grundlage); die Grundlage
des Researchers durchläuft die Minimierungsprüfung (TM-AC-9). Protokoll: `grundlage_aus`,
`[SEC] legal.resume.basis`. Protokoll: `wiederverwendet_aus`, `[SEC] legal.container.reused`;
abweichender Hash → `legal.resume.mismatch`, ungültige Quelle → Exit 2 `legal.resume.rejected`.

## Fehlersuche (`--debug`)

`legal.bash --debug` schreibt stdout und stderr jedes Containers nach
`/tmp/legal-…/NNN_rolle_familie/stdout.txt` bzw. `stderr.txt` (0600; `claude -p` meldet z. B.
Limits auf stdout), bei Codex zusätzlich
`…/home/codex-stderr.txt`, setzt `RUST_BACKTRACE=1` und behält das Arbeitsverzeichnis. Bei
rc≠0 nennt das Terminal nur den Pfad, nie den Inhalt (TM-AC-21). Die Dateien können
pseudonymisierten Akteninhalt enthalten; nach der Fehlersuche löschen. Codex-Logs eines
behaltenen Laufs liest `python3 -I rechtsberatung/diagnose_codex.py <…/home/.codex> [--details]`.

**Konto-Erweiterungen sind abgeschaltet:** Codex läuft mit `--disable apps|plugins|remote_plugin|
plugin_discovery_prefetch|tool_suggest`, ohne System-Skills und Memories; Claude mit
`--strict-mcp-config`, `ENABLE_CLAUDEAI_MCP_SERVERS=false` und `disableClaudeAiConnectors` im
Container-Home. Codex-Ausgaben gehen nicht in Docker-Pipes (Panic rc=101, openai/codex#55242).

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
Gesamtnote, Gleichstand → Claude → Stratege (nur `Vergleich: ja`) →
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
| `LEGAL_RESEARCH_DOMAINS` (ergänzt) | Liste aus E-4; nur noch bevorzugte Quellen und klickbare Links im Ergebnis, keine Netzsperre |
| `LEGAL_PSEUDONYM_REGEX` | `<[A-ZÄÖÜ_]+_?\d+>\|\b[A-ZÄÖÜ]{3,}_\d+\b` |
| `LEGAL_MAPPING_NAMEN` | zusätzlich zu `mapping.md` |
| `LEGAL_LOG_LEVEL` | `INFO` |

Rollen: `sachverhalt, rechtsfragen, researcher, registercheck, anwalt, review, ueberarbeitung,
richter, gegenanwalt, persona_klaeger, persona_beklagter, stratege, groesse, coldread`.
Modell-IDs müssen `^[a-z0-9][a-z0-9.\-]{1,63}$` erfüllen; jeder Override loggt
`[SEC] event=legal.model.override`. Ungültige Werte → Exit 2.

## Image bauen

```bash
docker build -t legal:1 rechtsberatung/docker
```

Basis per Digest, CLIs per `npm ci` aus `package-lock.json` (Version und Integrity),
setuid/setgid-Bits entfernt, kein ssh/sudo, `USER node` (UID 1000). Das System-CA-Bundle
`/etc/ssl/certs/ca-certificates.crt` (`SSL_CERT_FILE`) wird beim Build aus Nodes eingebauten
Mozilla-Wurzeln erzeugt (root-eigen, 0444); die Codex-CLI braucht es für TLS, das slim-Image
bringt kein `ca-certificates` mit. Nach Änderungen am Dockerfile das Image neu bauen; die neue
Image-ID steht im Protokoll jedes Laufs. Der Egress-Proxy
(`egress-proxy.js`, nur Node-Standardbibliothek) läuft aus demselben Image.

Laufzeit: `LEGAL_IMAGE` wird zu Laufbeginn per `docker image inspect` auf die unveränderliche
Image-ID aufgelöst; alle Container laufen über diese ID, sie steht in `protokoll.md` und
`protokoll.jsonl`. Je Lauf entstehen zwei `docker network create --internal`-Netze (`api`,
`recherche`) mit je einem Proxy-Container; die Proxys hängen zusätzlich an einem eigenen,
nicht internen Egress-Netz des Laufs (nicht am Default-Bridge-Netz). Rollen-Container starten
erst, wenn beide Proxys auf Port 3128 annehmen. Beide Proxys erlauben nur `CONNECT` auf
Port 443 zu Hostnamen (keine IP-Literale), sperren private Zieladressen und loggen Ablehnungen als
`[SEC] event=legal.egress.denied role=<r> domain=<d>`. Der `api`-Proxy lässt nur die LLM-Domains
durch. Der `recherche`-Proxy (Researcher und Register-Bestätiger) ist nach Entscheidung von Timo
(2026-11-16) **für alle öffentlichen Domains offen** (`LEGAL_ALLOW=*`). Fundstellen von
beliebigen Domains können im Register `geprüft` werden; im Ergebnis bleiben nur Links auf
`LEGAL_RESEARCH_DOMAINS` klickbar, andere erscheinen entschärft als Text ohne Query.

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
Logs enthalten keine Akten-, Prompt- oder Modellinhalte und keine Dateinamen der Akte
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
| Basis `node:22.23.4-bookworm-slim` | `@sha256:5ec23b7c…3d96d` | Docker Official Image; Tag ≥ 7 Tage alt; läuft als `node` (UID 1000); setuid/setgid-Bits werden im Build entfernt; CA-Bundle aus Nodes Wurzelbestand statt apt-Paket (keine zusätzliche Abhängigkeit, 2026-11-16) |
| `@anthropic-ai/claude-code` | 2.1.325 (2026-11-13) | **Release-Cooldown unterschritten (4 Tage) – Entscheidung Timo 2026-11-17 („nutze die neuere version“)**, weil 2.1.319 `claude-fable-5-2` nicht kennt (stderr `unrecognized_model`, nimmt nur 200k Kontext an); im Binary von 2.1.325 ist die Modell-ID enthalten. OSV: keine Funde für Haupt- und Plattformpakete (geprüft 2026-11-17); npm-Registry-Signaturen aller 5 Pakete verifiziert, 2 mit Attestation; Integrity im Lockfile = Registry-Wert; neuere 2.1.326 (2026-11-16) nicht übernommen; Scorecard: kein Projekt (Repo ohne Quellcode) → erweiterte Prüfung: Publisher Anthropic, offizieller Scope `@anthropic-ai`, bereits im Vorbild genutzt. Hat ein `postinstall` (`install.cjs`) zum Verlinken des Plattform-Binärpakets, daher `npm ci` ohne `--ignore-scripts`. |
| `@openai/codex` | 0.167.0 (2026-11-06) | OSV: keine Funde (geprüft 2026-11-16); neuere 0.169.0 (2026-11-13) < 7 Tage alt; Scorecard `openai/codex` 6 |

| `python3` (Debian Bookworm) | `python3=3.11.2-1+b1`, `python3.11=3.11.2-6+deb12u9` | Offizielles Debian-Archiv der Basis, Signaturprüfung durch apt, exakt gepinnt, `--no-install-recommends`, kein pip. Zweck: Hilfsskripte von Codex. Ein Debian-Sicherheitsupdate bricht den Build ab → Pin bewusst anheben (Stand 2026-11-16). |

Plattform-Binärpakete beider CLIs stehen mit Integrity im Lockfile.

## Offene Betriebspunkte

- **E-3:** API-Konten mit DPA, ohne Training, eigenes Credential mit Ausgabelimit (Timo).
- **E-5:** Art.-9-Inhalte und Vertraulichkeitsabreden vor jedem Lauf prüfen (fester Punkt in `entscheidungen.md`).
- **E-6:** Festplattenverschlüsselung, Fallordner nicht in Cloud-Sync (fester Punkt in `entscheidungen.md`).
- **Malware- und Schwachstellenscan des Images** (supply-chain.md, Must-have 1) ist offen: In der Implementierungsumgebung gab es kein Docker. Vor der ersten Nutzung z. B. mit Trivy/Grype prüfen und den Digest bestätigen.
- **Docker-Integrationstests** (`tests/integration/`) sind nicht gelaufen (kein Docker); vor der ersten Nutzung mit `LEGAL_DOCKER_TESTS=1` ausführen.
- **Behoben 2026-11-16:** Codex brach im Container mit rc=1 ab (jede TLS-Verbindung scheiterte),
  weil das Image kein System-CA-Bundle hatte; mit Host-Zertifikaten lief Codex (rc=0). Nicht
  verifiziert ist weiterhin, ob Codex den Egress-Proxy (`HTTPS_PROXY`) für alle Verbindungen nutzt.
- **CLI-Verhalten im Container nicht live verifiziert:** u. a. `claude --add-dir /in` mit `--permission-mode acceptEdits`, Codex-Abruf von URLs über den Proxy (`NODE_USE_ENV_PROXY=1`, kein `curl` im Image) und die Schreibbarkeit des Container-Homes für die CLIs.
- WebSearch von Claude (Researcher) läuft serverseitig beim Anbieter und umgeht den Proxy (R-6, E-2).
