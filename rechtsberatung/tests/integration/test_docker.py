"""Docker-Integrationstests (TM-AC-6, -7, -20, -25). Nur mit `LEGAL_DOCKER_TESTS=1` und gebautem Image.

Voraussetzung: `docker build -t legal:1 rechtsberatung/docker`. Ohne die Variable werden
alle Tests übersprungen (siehe README, „offene Betriebspunkte“).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
import time
import unittest
from pathlib import Path

from rechtsberatung.config import lade_konfig
from rechtsberatung.container import Aufruf, DockerRunner, Netzwerke, baue_docker_argv, netz_fuer_rolle

AKTIV = os.environ.get("LEGAL_DOCKER_TESTS") == "1"
IMAGE = os.environ.get("LEGAL_IMAGE", "legal:1")


def docker(*args: str, timeout: float = 120, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], input=stdin, stdout=subprocess.PIPE,  # nosec B603 B607
                          stderr=subprocess.PIPE, timeout=timeout, check=False)


@unittest.skipUnless(AKTIV, "Docker-Integrationstests nur mit LEGAL_DOCKER_TESTS=1")
class ImageTest(unittest.TestCase):
    def test_cli_versionen_entsprechen_pins(self) -> None:
        lock = json.loads((Path(__file__).resolve().parents[2] / "docker" / "package-lock.json").read_text())
        pins = {"claude": lock["packages"]["node_modules/@anthropic-ai/claude-code"]["version"],
                "codex": lock["packages"]["node_modules/@openai/codex"]["version"]}
        for cli, version in pins.items():
            out = docker("run", "--rm", IMAGE, cli, "--version").stdout.decode()
            self.assertIn(version, out, cli)

    def test_keine_setuid_binaries_kein_ssh_sudo(self) -> None:
        out = docker("run", "--rm", IMAGE, "sh", "-c", "find / -xdev -perm /6000 -type f 2>/dev/null").stdout
        self.assertEqual(out.decode().strip(), "")
        self.assertNotEqual(docker("run", "--rm", IMAGE, "sh", "-c", "command -v ssh").returncode, 0)
        self.assertNotEqual(docker("run", "--rm", IMAGE, "sh", "-c", "command -v sudo").returncode, 0)

    def test_ca_bundle_vorhanden_und_nicht_beschreibbar(self) -> None:
        # Codex (Rust) braucht das System-CA-Bundle; ohne es scheitert jede TLS-Verbindung.
        skript = ('test -n "$SSL_CERT_FILE" && test "$(grep -c "BEGIN CERTIFICATE" "$SSL_CERT_FILE")" -ge 100 '
                  '&& echo ok')
        self.assertEqual(docker("run", "--rm", IMAGE, "sh", "-c", skript).stdout.decode().strip(), "ok")
        # Negativtest: Der Laufzeitnutzer darf keine eigene Wurzel-CA einschleusen.
        self.assertNotEqual(docker("run", "--rm", IMAGE, "sh", "-c",
                                   'test -w "$SSL_CERT_FILE" || test -w /etc/ssl/certs').returncode, 0)

    def test_python3_vorhanden_ohne_pip(self) -> None:
        out = docker("run", "--rm", IMAGE, "python3", "-c", "import json, re, sys; print(sys.version_info[:2])")
        self.assertEqual(out.stdout.decode().strip(), "(3, 11)")
        # Negativtest: kein Paketmanager für Laufzeit-Nachinstallationen im Image.
        self.assertNotEqual(docker("run", "--rm", IMAGE, "sh", "-c", "command -v pip3 || command -v pip").returncode,
                            0)

    def test_nicht_root(self) -> None:
        self.assertEqual(docker("run", "--rm", IMAGE, "id", "-u").stdout.decode().strip(), "1000")


@unittest.skipUnless(AKTIV, "Docker-Integrationstests nur mit LEGAL_DOCKER_TESTS=1")
class NetzUndIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lauf = f"it{int(time.time())}"
        k = lade_konfig({})
        self.netz = Netzwerke(self.lauf, image=IMAGE, llm_domains=k.llm_domains)
        self.netze = self.netz.__enter__()
        self.addCleanup(self.netz.__exit__, None, None, None)

    def _argv(self, rolle: str, befehl: list[str]) -> list[str]:
        for d in ("in", "out", "home"):
            (self.tmp / d).mkdir(exist_ok=True)
        a = Aufruf(rolle=rolle, familie="codex", netz="recherche" if rolle == "researcher" else "api",
                   in_dir=self.tmp / "in", out_dir=self.tmp / "out", home_dir=self.tmp / "home", prompt="",
                   name=f"legal-{self.lauf}-{rolle}-1", timeout=30, lauf_id=self.lauf)
        argv = baue_docker_argv(a, image=IMAGE, netz=self.netze[a.netz])
        return argv[:argv.index(IMAGE) + 1] + befehl

    def test_gegenanwalt_erreicht_web_nicht(self) -> None:
        js = "fetch('https://example.org').then(()=>process.exit(0)).catch(()=>process.exit(3))"
        res = subprocess.run(self._argv("gegenanwalt", ["node", "-e", js]),  # nosec B603
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        self.assertNotEqual(res.returncode, 0)

    def test_researcher_nur_allowlist(self) -> None:
        js = "fetch('https://example.org').then(()=>process.exit(0)).catch(()=>process.exit(3))"
        res = subprocess.run(self._argv("researcher", ["node", "-e", js]),  # nosec B603
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        self.assertNotEqual(res.returncode, 0)

    def test_eingaben_read_only_und_host_konfig_unveraendert(self) -> None:
        host = Path.home()
        vorher = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  (host / ".claude" / "settings.json", host / ".codex" / "config.toml") if p.is_file()}
        (self.tmp / "in").mkdir(exist_ok=True)
        (self.tmp / "in" / "endfassung.md").write_text("x", encoding="utf-8")
        skript = ("echo hook > /in/endfassung.md; mkdir -p ~/.claude ~/.codex; "
                  "echo '{\"hooks\":{}}' > ~/.claude/settings.json; echo 'notify=[\"x\"]' > ~/.codex/config.toml")
        subprocess.run(self._argv("coldread", ["sh", "-c", skript]), stdout=subprocess.PIPE,  # nosec B603
                       stderr=subprocess.PIPE, timeout=60, check=False)
        self.assertEqual((self.tmp / "in" / "endfassung.md").read_text(encoding="utf-8"), "x")
        nachher = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in vorher}
        self.assertEqual(vorher, nachher)

    def test_timeout_beendet_container(self) -> None:
        for d in ("in", "out", "home"):
            (self.tmp / d).mkdir(exist_ok=True)
        netz = netz_fuer_rolle("richter")
        a = Aufruf(rolle="richter", familie="codex", netz=netz,
                   in_dir=self.tmp / "in", out_dir=self.tmp / "out", home_dir=self.tmp / "home", prompt="",
                   name=f"legal-{self.lauf}-richter-9", timeout=3, lauf_id=self.lauf)

        def popen(argv: list[str], **kw: object) -> subprocess.Popen[bytes]:
            argv = argv[:argv.index(IMAGE) + 1] + ["sleep", "120"]
            return subprocess.Popen(argv, **kw)  # nosec B603

        res = DockerRunner(image=IMAGE, netze=self.netze, env={"OPENAI_API_KEY": "x"}, popen=popen)(a)
        self.assertTrue(res.timed_out)
        ps = docker("ps", "-q", "--filter", f"name=legal-{self.lauf}-richter-9").stdout.decode().strip()
        self.assertEqual(ps, "")


if __name__ == "__main__":
    unittest.main()
