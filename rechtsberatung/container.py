"""Gehärtete Rollen-Container, Egress-Netze und Docker-Runner (TM-AC-6, -7, -10, -20).

Jeder Modellaufruf läuft in einem kurzlebigen Container. Eingaben sind `:ro`
gemountet, genau ein leeres `/out` ist beschreibbar, das Home ist ein
temporäres Verzeichnis je Container. Der Prompt geht ausschließlich über stdin.
Netzverkehr läuft nur über den Egress-Proxy des jeweiligen internen Netzes.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
# subprocess nur mit list-argv und shell=False (python.md).
import subprocess  # nosec B404
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .config import FAMILIEN
from .logging_setup import progress, sec_event

PROXY_PORT = 3128
NETZE = ("api", "recherche")
RECHERCHE_ROLLEN = ("researcher", "registercheck")
CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep"
CLAUDE_WEB_TOOLS = "WebFetch,WebSearch"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,127}$")
_CRED_MAX = 64 * 1024
# Container-internes tmpfs, kein Host-Pfad (Bandit B108 ist hier ein Fehlalarm).
CONTAINER_TMPFS = "/tmp:rw,noexec,nosuid,size=256m"  # nosec B108

# Docker-Container-Pfade; Quellen kommen nur aus Arbeitsverzeichnissen des Laufs.
VERBOTENE_QUELLEN = (".ssh", "docker.sock", ".claude", ".claude.json", ".codex")


@dataclass(frozen=True)
class Aufruf:
    """Ein Rollen-Containerlauf."""

    rolle: str
    familie: str
    netz: str
    in_dir: Path
    out_dir: Path
    home_dir: Path
    prompt: str
    name: str
    timeout: int
    modell: str = ""
    lauf_id: str = ""


@dataclass(frozen=True)
class RunErgebnis:
    """Rückgabe eines Runners; stdout/stderr werden nie geloggt."""

    rc: int
    timed_out: bool = False


@dataclass(frozen=True)
class Netz:
    """Internes Docker-Netz und Hostname des zugehörigen Egress-Proxys."""

    name: str
    proxy: str


def netz_fuer_rolle(rolle: str) -> str:
    """Nur Researcher und Register-Bestätiger erhalten das Recherche-Netz (TM-AC-7)."""
    return "recherche" if rolle in RECHERCHE_ROLLEN else "api"


def _pruefe_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise ValueError("ungültiger Container- oder Netzname")
    return name


def _pruefe_quelle(pfad: Path) -> str:
    """Mount-Quelle muss absolut sein und darf keine Host-Konfiguration referenzieren."""
    p = Path(pfad)
    if not p.is_absolute():
        raise ValueError("Mount-Quelle muss absolut sein")
    if any(teil in VERBOTENE_QUELLEN for teil in p.parts):
        raise ValueError("verbotene Mount-Quelle")
    s = str(p)
    if "," in s or ":" in s:
        raise ValueError("unzulässiges Zeichen in Mount-Quelle")
    return s


def baue_docker_argv(aufruf: Aufruf, *, image: str, netz: Netz, env_namen: Sequence[str] = ()) -> list[str]:
    """Gehärteten `docker run`-Aufruf als list-argv bauen (ohne Prompt, ohne Secret-Werte)."""
    if aufruf.familie not in FAMILIEN:
        raise ValueError("unbekannte Modellfamilie")
    if aufruf.netz not in NETZE or aufruf.netz != netz_fuer_rolle(aufruf.rolle):
        raise ValueError("Netz passt nicht zur Rolle")
    for n in env_namen:
        if n not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            raise ValueError("unzulässige Umgebungsvariable")
    proxy_url = f"http://{_pruefe_name(netz.proxy)}:{PROXY_PORT}"
    argv = [
        "docker", "run", "--rm", "-i",
        "--read-only",
        "--tmpfs", CONTAINER_TMPFS,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "512",
        "--memory", "4g",
        "--cpus", "2",
        "--user", f"{os.getuid()}:{os.getgid()}",   # Host-Nutzer: Arbeitsverzeichnisse sind 0700
        "--name", _pruefe_name(aufruf.name),
        "--label", f"legal.run={_pruefe_name(aufruf.lauf_id)}",
        "--network", _pruefe_name(netz.name),
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", f"https_proxy={proxy_url}",
        "-e", f"http_proxy={proxy_url}",
        "-e", "NODE_USE_ENV_PROXY=1",
        "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "-e", "DISABLE_AUTOUPDATER=1",
        "-e", "HOME=/home/node",
    ]
    for n in env_namen:
        argv += ["-e", n]   # nur Name: der Wert kommt aus der Host-Umgebung, nie ins argv
    argv += [
        "-v", f"{_pruefe_quelle(aufruf.in_dir)}:/in:ro",
        "-v", f"{_pruefe_quelle(aufruf.out_dir)}:/out:rw",
        "-v", f"{_pruefe_quelle(aufruf.home_dir)}:/home/node:rw",
        "-w", "/out",
        image,
    ]
    if aufruf.familie == "claude":
        recherche = aufruf.rolle == "researcher"
        if not aufruf.modell:
            raise ValueError("Claude braucht eine Modell-ID")
        argv += [
            "claude", "-p", "--model", aufruf.modell,
            "--permission-mode", "acceptEdits",
            "--add-dir", "/in",
            "--allowedTools", CLAUDE_TOOLS + ("," + CLAUDE_WEB_TOOLS if recherche else ""),
            "--disallowedTools", "Bash" + ("" if recherche else "," + CLAUDE_WEB_TOOLS),
        ]
    else:
        # Die Codex-eigene Sandbox funktioniert im gehärteten Container nicht; die
        # Isolation leisten Container, Mounts und Egress-Proxy (Design Notes).
        argv += ["codex", "exec", "--skip-git-repo-check", "--sandbox", "danger-full-access", "-C", "/out"]
        if aufruf.modell:
            argv += ["--model", aufruf.modell]
        argv.append("-")   # Prompt von stdin
    return argv


def _kopiere_credential(quelle: Path, ziel: Path) -> bool:
    """Auth-Datei (reguläre Datei, kein Symlink, ≤ 64 KiB) mit 0600 ins temporäre Home kopieren."""
    try:
        fd = os.open(quelle, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > _CRED_MAX:
            return False
        daten = fh.read(_CRED_MAX + 1)
    ziel.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    wfd = os.open(ziel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(wfd, "wb") as fh:
        fh.write(daten)
    return True


def bereite_credentials_vor(familie: str, home_dir: Path, env: Mapping[str, str],
                            host_home: Path) -> tuple[list[str], list[Path]]:
    """API-Key per Name bevorzugen (E-3), sonst Kopie der Auth-Datei ins Container-Home.

    Rückgabe: (Env-Namen für `-e`, kopierte Dateien zum Löschen nach dem Lauf).
    """
    if familie == "claude":
        if env.get("ANTHROPIC_API_KEY"):
            return ["ANTHROPIC_API_KEY"], []
        quelle, ziel = host_home / ".claude" / ".credentials.json", home_dir / ".claude" / ".credentials.json"
    else:
        if env.get("OPENAI_API_KEY"):
            return ["OPENAI_API_KEY"], []
        quelle, ziel = host_home / ".codex" / "auth.json", home_dir / ".codex" / "auth.json"
    if _kopiere_credential(quelle, ziel):
        return [], [ziel]
    sec_event("legal.credentials.missing", "warn", komponente="container", familie=familie)
    return [], []


def _docker() -> str:
    return shutil.which("docker") or "docker"


def _run(argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    # list-argv, shell=False; argv stammt ausschließlich aus validierten Namen (_pruefe_name).
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,  # nosec B603
                          timeout=timeout, check=False)


def kill_container(name: str, run: Callable[..., object] = _run) -> None:
    """Container hart beenden (`docker kill <name>`)."""
    try:
        run([_docker(), "kill", _pruefe_name(name)], timeout=30)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def kill_lauf(lauf_id: str, run: Callable[..., subprocess.CompletedProcess[bytes]] = _run) -> int:
    """Alle Container mit Label `legal.run=<lauf_id>` beenden (SIGINT/SIGTERM, TM-AC-20)."""
    try:
        res = run([_docker(), "ps", "-q", "--filter", f"label=legal.run={_pruefe_name(lauf_id)}"], timeout=30)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
    ids = [z for z in res.stdout.decode("ascii", "ignore").split() if re.fullmatch(r"[a-f0-9]{6,64}", z)]
    for cid in ids:
        try:
            run([_docker(), "kill", cid], timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
    return len(ids)


class DockerRunner:
    """Führt einen `Aufruf` per `docker run` aus (subprocess ohne Shell, Prompt über stdin)."""

    def __init__(self, *, image: str, netze: Mapping[str, Netz], env: Mapping[str, str] | None = None,
                 host_home: Path | None = None,
                 popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
                 run: Callable[..., object] = _run) -> None:
        self.image = image
        self.netze = netze
        self.env = os.environ if env is None else env
        self.host_home = host_home or Path.home()
        self._popen = popen
        self._run = run

    def __call__(self, aufruf: Aufruf) -> RunErgebnis:
        env_namen, kopien = bereite_credentials_vor(aufruf.familie, aufruf.home_dir, self.env, self.host_home)
        try:
            argv = baue_docker_argv(aufruf, image=self.image, netz=self.netze[aufruf.netz], env_namen=env_namen)
            argv[0] = _docker()
            try:
                proc = self._popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            except OSError:
                return RunErgebnis(rc=127)
            try:
                proc.communicate(input=aufruf.prompt.encode("utf-8"), timeout=aufruf.timeout)
            except subprocess.TimeoutExpired:
                kill_container(aufruf.name, self._run)
                proc.kill()
                try:
                    proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                sec_event("legal.timeout.killed", "aborted", komponente="container", rolle=aufruf.rolle,
                          familie=aufruf.familie, timeout_s=aufruf.timeout)
                return RunErgebnis(rc=124, timed_out=True)
            return RunErgebnis(rc=int(proc.returncode or 0))
        finally:
            for k in kopien:
                try:
                    k.unlink()
                except OSError:
                    pass


_DENIED_RE = re.compile(r"^\[SEC\] event=legal\.egress\.denied role=(api|recherche) domain=([a-z0-9.\-]{1,253}|invalid)$")


def parse_proxy_log(text: str) -> list[tuple[str, str]]:
    """Proxy-Logzeilen streng parsen; nur exakt passende Zeilen werden übernommen."""
    treffer = []
    for zeile in text.splitlines()[:10000]:
        m = _DENIED_RE.fullmatch(zeile.strip())
        if m:
            treffer.append((m.group(1), m.group(2)))
    return treffer


_BEREIT_JS = ("require('net').connect(3128,'127.0.0.1')"
              ".on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))")


class Netzwerke:
    """Kontextmanager: je Lauf zwei interne Netze mit je einem Egress-Proxy (E-1).

    `api` erlaubt nur die LLM-Domains; `recherche` (Researcher, Register-Bestätiger) ist offen für
    jeden öffentlichen Hostnamen auf Port 443.

    Die Proxys hängen zusätzlich an einem eigenen, nicht internen Egress-Netz des Laufs (nicht am
    Default-Bridge-Netz), damit fremde Container den Proxy nicht erreichen. Rollen-Container
    starten erst, wenn beide Proxys auf Port 3128 annehmen.
    """

    def __init__(self, lauf_id: str, *, image: str, llm_domains: Sequence[str],
                 run: Callable[..., subprocess.CompletedProcess[bytes]] = _run,
                 schlaf: Callable[[float], None] = time.sleep, bereit_versuche: int = 30) -> None:
        self.lauf_id = _pruefe_name(lauf_id)
        self.image = image
        self.allow = {
            "api": ",".join(llm_domains),
            # Entscheidung Timo 2026-11-16: Recherche-Proxy ohne Domain-Liste (E-4 geändert). Port 443,
            # Hostnamen statt IP-Literalen und die Sperre privater Adressen bleiben (egress-proxy.js).
            "recherche": "*",
        }
        self._run = run
        self._schlaf = schlaf
        self._versuche = bereit_versuche
        self.netze: dict[str, Netz] = {}
        self._angelegt: list[tuple[str, str]] = []

    def _docker(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[bytes]:
        res = self._run([_docker(), *args], timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError("docker-Befehl fehlgeschlagen")
        return res

    def _warte_bereit(self, proxy: str) -> None:
        for versuch in range(self._versuche):
            try:
                if self._run([_docker(), "exec", proxy, "node", "-e", _BEREIT_JS], timeout=30).returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            if versuch + 1 < self._versuche:
                self._schlaf(0.5)
        sec_event("legal.egress.unavailable", "aborted", komponente="egress-proxy")
        raise RuntimeError("Egress-Proxy nicht bereit")

    def __enter__(self) -> dict[str, Netz]:
        uid = f"{os.getuid()}:{os.getgid()}"
        label = f"legal.run={self.lauf_id}"
        try:
            egress = f"legal-{self.lauf_id}-egress"
            self._angelegt.append(("network", egress))
            self._docker("network", "create", "--label", label, egress)
            for art in NETZE:
                netz = f"legal-{self.lauf_id}-{art}"
                proxy = f"legal-{self.lauf_id}-proxy-{art}"
                self._angelegt.append(("network", netz))
                self._docker("network", "create", "--internal", "--label", label, netz)
                # Vor dem Start registrieren: auch ein halb angelegter Container wird entfernt.
                self._angelegt.append(("container", proxy))
                self._docker(
                    "run", "-d", "--name", proxy, "--label", label,
                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    "--pids-limit", "128", "--memory", "256m", "--cpus", "0.5", "--user", uid,
                    "--network", egress,
                    "-e", f"LEGAL_ALLOW={self.allow[art]}", "-e", f"LEGAL_PROXY_ROLE={art}",
                    self.image, "node", "/opt/legal/egress-proxy.js",
                )
                self._docker("network", "connect", netz, proxy)
                self.netze[art] = Netz(name=netz, proxy=proxy)
            for netz_info in self.netze.values():
                self._warte_bereit(netz_info.proxy)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self.netze

    def __exit__(self, *exc: object) -> None:
        for art, name in reversed(self._angelegt):
            if art == "container":
                try:
                    res = self._run([_docker(), "logs", name], timeout=60)
                    for rolle, domain in parse_proxy_log((res.stdout + b"\n" + res.stderr).decode("utf-8", "replace")):
                        sec_event("legal.egress.denied", "denied", komponente="egress-proxy", role=rolle, domain=domain)
                except (OSError, subprocess.SubprocessError):
                    pass
                try:
                    self._run([_docker(), "rm", "-f", name], timeout=60)
                except (OSError, subprocess.SubprocessError):
                    pass
            else:
                try:
                    self._run([_docker(), "network", "rm", name], timeout=60)
                except (OSError, subprocess.SubprocessError):
                    pass
        self._angelegt.clear()


def erstelle_signal_handler(lauf_id: str, kill: Callable[[str], int] = kill_lauf) -> Callable[[int, object], None]:
    """Handler für SIGINT/SIGTERM: einmalig alle Container des Laufs beenden, dann abbrechen.

    Weitere Signale werden danach ignoriert, damit Protokoll, `entscheidungen.md` und der Abbau
    der Netze nicht unterbrochen werden.
    """
    ausgeloest = {"ja": False}

    def _handler(signum: int, _frame: object) -> None:
        if ausgeloest["ja"]:
            return
        ausgeloest["ja"] = True
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, signal.SIG_IGN)
        progress("Signal empfangen – Container des Laufs werden beendet")
        n = kill(lauf_id)
        sec_event("legal.run.signal", "aborted", komponente="conductor", signal=signum, killed=n)
        raise KeyboardInterrupt
    return _handler


def installiere_signal_handler(lauf_id: str) -> None:
    """SIGINT/SIGTERM: alle Container des Laufs beenden, dann regulär abbrechen (TM-AC-20)."""
    handler = erstelle_signal_handler(lauf_id)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


_IMAGE_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def ermittle_image_id(image: str, run: Callable[..., subprocess.CompletedProcess[bytes]] = _run) -> str:
    """Bewegliches Tag einmal je Lauf auf die unveränderliche Image-ID auflösen (TM-AC-19, -25)."""
    try:
        res = run([_docker(), "image", "inspect", "--format", "{{.Id}}", image], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Image nicht auflösbar") from exc
    image_id = res.stdout.decode("ascii", "ignore").strip()
    if res.returncode != 0 or not _IMAGE_ID_RE.fullmatch(image_id):
        raise RuntimeError("Image nicht auflösbar")
    return image_id


def credentials_vorhanden(familie: str, env: Mapping[str, str], host_home: Path) -> bool:
    """Preflight (E-3): API-Key im Env oder reguläre Auth-Datei (kein Symlink) vorhanden."""
    if familie == "claude":
        name, datei = "ANTHROPIC_API_KEY", host_home / ".claude" / ".credentials.json"
    else:
        name, datei = "OPENAI_API_KEY", host_home / ".codex" / "auth.json"
    if env.get(name):
        return True
    try:
        st = os.lstat(datei)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and 0 < st.st_size <= _CRED_MAX
