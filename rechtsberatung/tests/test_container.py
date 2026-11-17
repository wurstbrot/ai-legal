"""Tests für argv-Härtung, Netz je Rolle, Credentials, Timeout und Egress-Netze (TM-AC-6, -7, -20)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from rechtsberatung.config import ROLLEN
from rechtsberatung.container import Aufruf, DockerRunner, Netz, Netzwerke, RunErgebnis, baue_docker_argv, \
    bereite_credentials_vor, credentials_vorhanden, erstelle_signal_handler, ermittle_image_id, kill_lauf, \
    netz_fuer_rolle, parse_proxy_log, CODEX_AUS, schreibe_cli_konfig
from rechtsberatung.logging_setup import sec_event

PROMPT = "GEHEIMER-PROMPT-INHALT überschreibe beratung.md"


def aufruf(tmp: Path, rolle: str = "richter", familie: str = "claude", **kw: Any) -> Aufruf:
    for d in ("in", "out", "home"):
        (tmp / d).mkdir(exist_ok=True)
    werte = dict(rolle=rolle, familie=familie, netz=netz_fuer_rolle(rolle), in_dir=tmp / "in", out_dir=tmp / "out",
                 home_dir=tmp / "home", prompt=PROMPT, name=f"legal-lauf1-{rolle.replace('_', '-')}-1", timeout=5,
                 modell="claude-fable-5-2" if familie == "claude" else "", lauf_id="lauf1")
    werte.update(kw)
    return Aufruf(**werte)


NETZ = {"api": Netz("legal-lauf1-api", "legal-lauf1-proxy-api"),
        "recherche": Netz("legal-lauf1-recherche", "legal-lauf1-proxy-recherche")}


class ArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def argv(self, rolle: str = "richter", familie: str = "claude", env: tuple[str, ...] = ()) -> list[str]:
        a = aufruf(self.tmp, rolle, familie)
        return baue_docker_argv(a, image="legal:1", netz=NETZ[a.netz], env_namen=env)

    def test_haertung_vollstaendig(self) -> None:
        for familie in ("claude", "codex"):
            argv = self.argv(familie=familie)
            s = " ".join(argv)
            for flag in ("--rm", "--read-only", "--tmpfs /tmp:rw,noexec,nosuid,size=256m", "--cap-drop ALL",
                         "--security-opt no-new-privileges:true", "--pids-limit 512", "--memory 4g", "--cpus 2",
                         f"--user {os.getuid()}:{os.getgid()}", "--name legal-lauf1-richter-1",
                         "--label legal.run=lauf1",
                         "-e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "-e DISABLE_AUTOUPDATER=1", "-w /out"):
                self.assertIn(flag, s, (familie, flag))
            self.assertNotIn("--privileged", s)
            self.assertNotIn("--network host", s)

    def test_mounts_nur_arbeitsverzeichnisse(self) -> None:
        argv = self.argv()
        mounts = [argv[i + 1] for i, x in enumerate(argv) if x == "-v"]
        self.assertEqual(mounts, [f"{self.tmp / 'in'}:/in:ro", f"{self.tmp / 'out'}:/out:rw",
                                  f"{self.tmp / 'home'}:/home/node:rw"])
        s = " ".join(argv)
        for verboten in (".ssh", "docker.sock", "/.claude", ".claude.json", "/.codex"):
            self.assertNotIn(verboten, s)

    def test_fallordner_nur_lesbar_fuer_api_rollen(self) -> None:
        fall = self.tmp / "fall"
        for familie in ("claude", "codex"):
            with self.subTest(familie=familie):
                a = aufruf(self.tmp, "richter", familie, fall_dir=fall)
                argv = baue_docker_argv(a, image="legal:1", netz=NETZ["api"])
                mounts = [argv[i + 1] for i, x in enumerate(argv) if x == "-v"]
                self.assertIn(f"{fall}:/fall:ro", mounts)
                self.assertFalse(any(m.startswith(f"{fall}:") and not m.endswith(":ro") for m in mounts))
                self.assertLess(argv.index(f"{fall}:/fall:ro"), argv.index("legal:1"))
                if familie == "claude":
                    i = argv.index("legal:1")
                    self.assertIn("/fall", argv[i:])

    def test_fallordner_nie_fuer_recherche_rollen(self) -> None:
        for rolle in ("researcher", "registercheck"):
            with self.subTest(rolle=rolle), self.assertRaises(ValueError):
                a = aufruf(self.tmp, rolle, "claude", fall_dir=self.tmp / "fall")
                baue_docker_argv(a, image="legal:1", netz=NETZ["recherche"])

    def test_fallordner_verbotene_quelle_abgelehnt(self) -> None:
        for quelle in (Path.home() / ".ssh", Path.home() / ".claude", Path("relativ"), Path("/srv/a:b"),
                       Path("/srv/a,b")):
            with self.subTest(quelle=quelle), self.assertRaises(ValueError):
                baue_docker_argv(aufruf(self.tmp, "richter", "codex", fall_dir=quelle), image="legal:1",
                                 netz=NETZ["api"])

    def test_verbotene_mount_quellen_werden_abgelehnt(self) -> None:
        for quelle in (Path.home() / ".ssh", Path("/var/run/docker.sock"), Path.home() / ".claude",
                       Path.home() / ".codex", Path("relativ"), Path("/srv/a:b")):
            with self.subTest(quelle=quelle), self.assertRaises(ValueError):
                a = aufruf(self.tmp)
                baue_docker_argv(Aufruf(**{**a.__dict__, "in_dir": quelle}), image="legal:1", netz=NETZ["api"])

    def test_prompt_nie_im_argv(self) -> None:
        for familie in ("claude", "codex"):
            for rolle in ROLLEN:
                with self.subTest(rolle=rolle, familie=familie):
                    s = " ".join(self.argv(rolle, familie))
                    self.assertNotIn("GEHEIMER-PROMPT", s)
                    self.assertNotIn("beratung.md", s)

    def test_netz_je_rolle(self) -> None:
        for rolle in ROLLEN:
            argv = self.argv(rolle)
            netz = argv[argv.index("--network") + 1]
            if rolle in ("researcher", "registercheck"):
                self.assertEqual(netz, "legal-lauf1-recherche", rolle)
            else:
                self.assertEqual(netz, "legal-lauf1-api", rolle)
            self.assertIn(f"HTTPS_PROXY=http://{NETZ[netz_fuer_rolle(rolle)].proxy}:3128", argv)

    def test_netz_passend_zur_rolle_erzwungen(self) -> None:
        a = aufruf(self.tmp, "richter")
        with self.assertRaises(ValueError):
            baue_docker_argv(Aufruf(**{**a.__dict__, "netz": "recherche"}), image="legal:1", netz=NETZ["recherche"])

    def test_claude_tools(self) -> None:
        argv = self.argv("researcher")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Write,Edit,Glob,Grep,WebFetch,WebSearch")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Bash")
        for rolle in ROLLEN:
            if rolle == "researcher":
                continue
            argv = self.argv(rolle)
            self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Write,Edit,Glob,Grep", rolle)
            self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Bash,WebFetch,WebSearch", rolle)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_codex_argv_prompt_stdin(self) -> None:
        # Seit 2026-11-16: Ausgabe nicht in Docker-Pipes (openai/codex#55242), Konto-Erweiterungen aus.
        argv = self.argv(familie="codex")
        i = argv.index("legal:1")
        self.assertEqual(argv[i + 1:], ["sh", "-c", 'exec "$@" >/dev/null 2>/dev/null', "sh",
                                        "codex", "exec", *CODEX_AUS, "--skip-git-repo-check", "--sandbox",
                                        "danger-full-access", "-C", "/out", "-"])

    def test_codex_konto_erweiterungen_aus(self) -> None:
        argv = self.argv(familie="codex")
        for feature in ("apps", "plugins", "remote_plugin", "plugin_discovery_prefetch", "tool_suggest"):
            self.assertTrue(any(argv[j] == "--disable" and argv[j + 1] == feature for j in range(len(argv) - 1)),
                            feature)
        for wert in ("skills.bundled.enabled=false", "memories.use_memories=false"):
            self.assertIn(wert, argv)

    def test_codex_debug_stderr_in_datei_modell_nur_als_argument(self) -> None:
        a = aufruf(self.tmp, "richter", "codex", modell="gpt-x; rm -rf /")
        argv = baue_docker_argv(a, image="legal:1", netz=NETZ["api"], debug=True)
        i = argv.index("legal:1")
        self.assertEqual(argv[i + 3], 'exec "$@" >/dev/null 2>/home/node/codex-stderr.txt')
        # Werte stehen nur als eigene argv-Elemente hinter "$@", nie im Shell-Skript.
        self.assertNotIn("gpt-x", argv[i + 3])
        self.assertIn("gpt-x; rm -rf /", argv[i + 4:])

    def test_claude_konto_erweiterungen_aus(self) -> None:
        argv = self.argv(familie="claude")
        i = argv.index("legal:1")
        for wert in ("ENABLE_CLAUDEAI_MCP_SERVERS=false", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"):
            self.assertIn(wert, argv[:i])
        self.assertIn("--strict-mcp-config", argv[i:])
        codex = self.argv(familie="codex")
        self.assertNotIn("ENABLE_CLAUDEAI_MCP_SERVERS=false", codex)

    def test_api_key_nur_als_name(self) -> None:
        argv = self.argv(env=("ANTHROPIC_API_KEY",))
        self.assertIn("ANTHROPIC_API_KEY", argv)
        self.assertFalse(any(x.startswith("ANTHROPIC_API_KEY=") for x in argv))
        with self.assertRaises(ValueError):
            self.argv(env=("AWS_SECRET_ACCESS_KEY",))


class CredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = self.tmp / "hosthome"
        (self.host / ".claude").mkdir(parents=True)
        (self.host / ".codex").mkdir(parents=True)
        (self.host / ".claude" / ".credentials.json").write_text('{"t": "x"}', encoding="utf-8")
        (self.host / ".codex" / "auth.json").write_text('{"t": "y"}', encoding="utf-8")
        self.home = self.tmp / "home"
        self.home.mkdir()

    def test_api_key_bevorzugt(self) -> None:
        namen, kopien = bereite_credentials_vor("claude", self.home, {"ANTHROPIC_API_KEY": "k"}, self.host)
        self.assertEqual((namen, kopien), (["ANTHROPIC_API_KEY"], []))
        namen, _ = bereite_credentials_vor("codex", self.home, {"OPENAI_API_KEY": "k"}, self.host)
        self.assertEqual(namen, ["OPENAI_API_KEY"])

    def test_kopie_0600_im_temporaeren_home(self) -> None:
        for familie, rel in (("claude", ".claude/.credentials.json"), ("codex", ".codex/auth.json")):
            namen, kopien = bereite_credentials_vor(familie, self.home, {}, self.host)
            self.assertEqual(namen, [])
            self.assertEqual(kopien, [self.home / rel])
            self.assertEqual(stat.S_IMODE(os.stat(self.home / rel).st_mode), 0o600)

    def test_claude_settings_schalten_connectoren_ab(self) -> None:
        schreibe_cli_konfig("claude", self.home)
        datei = self.home / ".claude" / "settings.json"
        daten = json.loads(datei.read_text(encoding="utf-8"))
        self.assertIs(daten["disableClaudeAiConnectors"], True)
        self.assertIs(daten["syncClaudeAiPlugins"], False)
        self.assertEqual(stat.S_IMODE(os.stat(datei).st_mode), 0o600)
        schreibe_cli_konfig("codex", self.tmp / "leer")
        self.assertFalse((self.tmp / "leer").exists())

    def test_claude_settings_folgen_keinem_symlink(self) -> None:
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        os.symlink(self.tmp / "fremd.json", self.home / ".claude" / "settings.json")
        with self.assertRaises(OSError):
            schreibe_cli_konfig("claude", self.home)
        self.assertFalse((self.tmp / "fremd.json").exists())

    def test_symlink_als_auth_datei_wird_nicht_kopiert(self) -> None:
        (self.host / ".codex" / "auth.json").unlink()
        os.symlink(self.tmp / "irgendwas", self.host / ".codex" / "auth.json")
        with self.assertLogs("legal", level="INFO"):
            namen, kopien = bereite_credentials_vor("codex", self.home, {}, self.host)
        self.assertEqual((namen, kopien), ([], []))


class _HaengenderProzess:
    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.eingabe: bytes | None = None

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
        if input is not None:
            self.eingabe = input
        if not self.killed:
            raise subprocess.TimeoutExpired("docker", timeout or 0)
        return b"", b""

    def kill(self) -> None:
        self.killed = True


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_timeout_ruft_docker_kill(self) -> None:
        aufrufe: list[list[str]] = []
        prozesse: list[_HaengenderProzess] = []
        argvs: list[list[str]] = []

        def popen(argv: list[str], **kw: Any) -> _HaengenderProzess:
            argvs.append(argv)
            p = _HaengenderProzess()
            prozesse.append(p)
            return p

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            aufrufe.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        runner = DockerRunner(image="legal:1", netze=NETZ, env={"ANTHROPIC_API_KEY": "k"}, host_home=self.tmp,
                              popen=popen, run=run)
        with self.assertLogs("legal", level="INFO") as cm:
            res = runner(aufruf(self.tmp))
        self.assertEqual(res, RunErgebnis(rc=124, timed_out=True))
        self.assertTrue(any(a[1:] == ["kill", "legal-lauf1-richter-1"] for a in aufrufe))
        self.assertTrue(any("event=legal.timeout.killed" in z for z in cm.output))
        self.assertEqual(prozesse[0].eingabe, PROMPT.encode("utf-8"))
        self.assertNotIn(PROMPT, " ".join(argvs[0]))

    def _runner_mit_stderr(self, debug: bool, rc: int) -> tuple[DockerRunner, list[dict[str, Any]], list[list[str]]]:
        kws: list[dict[str, Any]] = []
        argvs: list[list[str]] = []

        class Fertig:
            returncode = rc

            def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"", b""

        def popen(argv: list[str], **kw: Any) -> Fertig:
            kws.append(kw)
            argvs.append(argv)
            if hasattr(kw["stderr"], "write"):
                kw["stderr"].write(b"thread 'main' panicked at KANARIE-STDERR\n")
            if hasattr(kw["stdout"], "write"):
                kw["stdout"].write(b"Usage limit reached KANARIE-STDOUT\n")
            return Fertig()

        for d in ("in", "out", "home"):
            (self.tmp / d).mkdir(exist_ok=True)
        runner = DockerRunner(image="legal:1", netze=NETZ, env={"OPENAI_API_KEY": "k"}, host_home=self.tmp,
                              popen=popen, debug=debug)
        return runner, kws, argvs

    def test_ohne_debug_wird_stderr_verworfen(self) -> None:
        runner, kws, argvs = self._runner_mit_stderr(False, 101)
        with self.assertLogs("legal", level="DEBUG") as cm:
            sec_event("test.marker", "ok")
            self.assertEqual(runner(aufruf(self.tmp, "sachverhalt", "codex")).rc, 101)
        self.assertIs(kws[0]["stderr"], subprocess.DEVNULL)
        self.assertIs(kws[0]["stdout"], subprocess.DEVNULL)
        self.assertFalse((self.tmp / "stderr.txt").exists())
        self.assertFalse((self.tmp / "stdout.txt").exists())
        self.assertNotIn("RUST_BACKTRACE=1", argvs[0])
        self.assertFalse(any("stderr" in z for z in cm.output))

    def test_debug_schreibt_stderr_0600_nur_in_datei_nie_ins_log(self) -> None:
        runner, kws, argvs = self._runner_mit_stderr(True, 101)
        with self.assertLogs("legal", level="DEBUG") as cm:
            self.assertEqual(runner(aufruf(self.tmp, "sachverhalt", "codex")).rc, 101)
        datei = self.tmp / "stderr.txt"
        self.assertIn(b"KANARIE-STDERR", datei.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(datei).st_mode), 0o600)
        self.assertTrue(kws[0]["stderr"].closed)
        aus = self.tmp / "stdout.txt"
        self.assertIn(b"KANARIE-STDOUT", aus.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(aus).st_mode), 0o600)
        self.assertTrue(kws[0]["stdout"].closed)
        i = argvs[0].index("legal:1")
        self.assertIn("RUST_BACKTRACE=1", argvs[0][:i])
        alles = "\n".join(cm.output)
        self.assertNotIn("KANARIE-STDERR", alles)
        self.assertNotIn("KANARIE-STDOUT", alles)
        self.assertIn(str(datei), alles)
        self.assertIn(str(aus), alles)

    def test_debug_ohne_fehler_keine_meldung(self) -> None:
        runner, _, _ = self._runner_mit_stderr(True, 0)
        with self.assertLogs("legal", level="DEBUG") as cm:
            sec_event("test.marker", "ok")
            runner(aufruf(self.tmp, "sachverhalt", "codex"))
        self.assertFalse(any("Debug: Ausgaben" in z for z in cm.output))

    def test_debug_ueberschreibt_keine_vorhandene_datei_und_folgt_keinem_symlink(self) -> None:
        runner, kws, _ = self._runner_mit_stderr(True, 1)
        os.symlink(self.tmp / "ziel", self.tmp / "stderr.txt")
        with self.assertLogs("legal", level="INFO") as cm:
            self.assertEqual(runner(aufruf(self.tmp, "sachverhalt", "codex")).rc, 1)
        self.assertFalse((self.tmp / "ziel").exists())
        self.assertIs(kws[0]["stderr"], subprocess.DEVNULL)
        self.assertTrue(any("nicht anlegbar" in z for z in cm.output))

    def test_credential_kopie_wird_nach_lauf_geloescht(self) -> None:
        host = self.tmp / "hosthome"
        (host / ".claude").mkdir(parents=True)
        (host / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")

        class Fertig:
            returncode = 0

            def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"", b""

        a = aufruf(self.tmp)
        runner = DockerRunner(image="legal:1", netze=NETZ, env={}, host_home=host, popen=lambda *x, **k: Fertig(),
                              run=lambda *x, **k: None)
        self.assertEqual(runner(a).rc, 0)
        self.assertFalse((a.home_dir / ".claude" / ".credentials.json").exists())

    def test_kill_lauf_per_label(self) -> None:
        befehle: list[list[str]] = []

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            befehle.append(argv)
            out = b"abc123def456\n0123456789ab\n" if "ps" in argv else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

        self.assertEqual(kill_lauf("lauf1", run=run), 2)
        self.assertIn("label=legal.run=lauf1", befehle[0])
        self.assertEqual([b[1:] for b in befehle[1:]], [["kill", "abc123def456"], ["kill", "0123456789ab"]])


class NetzwerkeTest(unittest.TestCase):
    def test_interne_netze_proxy_und_abbau(self) -> None:
        befehle: list[list[str]] = []

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            befehle.append(argv[1:])
            out = b""
            if argv[1:3] == ["logs", "legal-lauf1-proxy-api"]:
                out = (b"[SEC] event=legal.egress.denied role=api domain=example.org\n"
                       b"[SEC] event=legal.egress.denied role=api domain=x.org/?d=geheim\n")
            return subprocess.CompletedProcess(argv, 0, out, b"")

        with self.assertLogs("legal", level="INFO") as cm:
            with Netzwerke("lauf1", image="legal:1", llm_domains=["api.anthropic.com"], run=run) as netze:
                self.assertEqual(netze["api"].name, "legal-lauf1-api")
        erstellt = [b for b in befehle if b[:2] == ["network", "create"]]
        self.assertEqual(len(erstellt), 3)
        self.assertEqual(erstellt[0][-1], "legal-lauf1-egress")
        self.assertNotIn("--internal", erstellt[0])   # nur das Egress-Netz der Proxys hat Ausgang
        self.assertTrue(all("--internal" in b for b in erstellt[1:]))
        self.assertTrue(all(b[-1] != "bridge" for b in befehle))
        proxies = [b for b in befehle if b[:2] == ["run", "-d"]]
        api = next(b for b in proxies if "LEGAL_PROXY_ROLE=api" in b)
        self.assertIn("LEGAL_ALLOW=api.anthropic.com", api)
        rech = next(b for b in proxies if "LEGAL_PROXY_ROLE=recherche" in b)
        self.assertIn("LEGAL_ALLOW=*", rech)   # Recherche offen (Entscheidung Timo 2026-11-16)
        for flag in ("--read-only", "no-new-privileges:true", "ALL", f"{os.getuid()}:{os.getgid()}"):
            self.assertIn(flag, api)
        self.assertEqual(api[api.index("--network") + 1], "legal-lauf1-egress")
        self.assertNotIn("bridge", api)
        # Bereitschaft beider Proxys wird vor der Rückgabe geprüft.
        bereit = [b for b in befehle if b[:1] == ["exec"]]
        self.assertEqual([b[1] for b in bereit], ["legal-lauf1-proxy-api", "legal-lauf1-proxy-recherche"])
        self.assertLess(befehle.index(bereit[-1]), befehle.index(["logs", "legal-lauf1-proxy-api"]))
        self.assertEqual(len([b for b in befehle if b[:2] == ["rm", "-f"]]), 2)
        self.assertEqual(len([b for b in befehle if b[:2] == ["network", "rm"]]), 3)
        denied = [z for z in cm.output if "event=legal.egress.denied" in z]
        self.assertEqual(len(denied), 1)
        self.assertIn("domain=example.org", denied[0])
        self.assertFalse(any("geheim" in z for z in cm.output))

    def test_abbau_auch_bei_fehler(self) -> None:
        befehle: list[list[str]] = []

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            befehle.append(argv[1:])
            rc = 1 if argv[1:3] == ["network", "connect"] else 0
            return subprocess.CompletedProcess(argv, rc, b"", b"")

        with self.assertRaises(RuntimeError):
            with Netzwerke("lauf1", image="legal:1", llm_domains=["a.com"], run=run):
                pass
        self.assertIn(["rm", "-f", "legal-lauf1-proxy-api"], befehle)
        self.assertIn(["network", "rm", "legal-lauf1-api"], befehle)
        self.assertIn(["network", "rm", "legal-lauf1-egress"], befehle)

    def test_proxy_nach_fehlgeschlagenem_start_entfernt(self) -> None:
        befehle: list[list[str]] = []

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            befehle.append(argv[1:])
            return subprocess.CompletedProcess(argv, 1 if argv[1:3] == ["run", "-d"] else 0, b"", b"")

        with self.assertRaises(RuntimeError):
            with Netzwerke("lauf1", image="legal:1", llm_domains=["a.com"], run=run):
                pass
        self.assertIn(["rm", "-f", "legal-lauf1-proxy-api"], befehle)

    def test_proxy_nicht_bereit(self) -> None:
        befehle: list[list[str]] = []
        pausen: list[float] = []

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            befehle.append(argv[1:])
            return subprocess.CompletedProcess(argv, 1 if argv[1] == "exec" else 0, b"", b"")

        with self.assertLogs("legal", level="INFO") as cm, self.assertRaisesRegex(RuntimeError, "nicht bereit"):
            with Netzwerke("lauf1", image="legal:1", llm_domains=["a.com"], run=run,
                           schlaf=pausen.append, bereit_versuche=4):
                self.fail("darf nicht erreicht werden")
        self.assertEqual(len([b for b in befehle if b[0] == "exec"]), 4)
        self.assertEqual(len(pausen), 3)
        self.assertTrue(any("event=legal.egress.unavailable" in z for z in cm.output))
        self.assertEqual(len([b for b in befehle if b[:2] == ["rm", "-f"]]), 2)

    def test_proxy_log_streng(self) -> None:
        text = ("[SEC] event=legal.egress.denied role=api domain=example.org\n"
                "[SEC] event=legal.egress.denied role=root domain=example.org\n"
                "[SEC] event=legal.egress.denied role=api domain=a.org extra=1\n"
                "irgendwas\n")
        self.assertEqual(parse_proxy_log(text), [("api", "example.org")])


class ModellArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_modell_nach_model_flag(self) -> None:
        a = aufruf(self.tmp, "richter", "codex", modell="gpt-5.5")
        argv = baue_docker_argv(a, image="legal:1", netz=NETZ["api"])
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.5")
        self.assertEqual(argv[-1], "-")
        ohne = baue_docker_argv(aufruf(self.tmp, "richter", "codex"), image="legal:1", netz=NETZ["api"])
        self.assertNotIn("--model", ohne)
        c = baue_docker_argv(aufruf(self.tmp, "richter", "claude", modell="claude-opus-5-5"), image="legal:1",
                             netz=NETZ["api"])
        self.assertEqual(c[c.index("--model") + 1], "claude-opus-5-5")
        with self.assertRaises(ValueError):
            baue_docker_argv(aufruf(self.tmp, "richter", "claude", modell=""), image="legal:1", netz=NETZ["api"])


class SignalImagePreflightTest(unittest.TestCase):
    def test_signal_handler_einmalig_und_ignoriert_weitere(self) -> None:
        alt = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in alt.items()])
        gekillt: list[str] = []
        handler = erstelle_signal_handler("lauf1", kill=lambda lid: gekillt.append(lid) or 3)
        with self.assertLogs("legal", level="INFO") as cm, self.assertRaises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
        self.assertEqual(gekillt, ["lauf1"])
        self.assertIs(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        handler(signal.SIGINT, None)   # zweites Signal: kein erneuter Abbruch, kein zweiter Kill
        self.assertEqual(gekillt, ["lauf1"])
        self.assertTrue(any("event=legal.run.signal" in z and "killed=3" in z for z in cm.output))

    def test_image_id(self) -> None:
        iid = "sha256:" + "a" * 64

        def run(argv: list[str], timeout: float = 0) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(argv[1:], ["image", "inspect", "--format", "{{.Id}}", "legal:1"])
            return subprocess.CompletedProcess(argv, 0, (iid + "\n").encode(), b"")
        self.assertEqual(ermittle_image_id("legal:1", run=run), iid)
        for rc, out in ((1, b""), (0, b"legal:1"), (0, b"sha256:xyz")):
            with self.subTest(out=out), self.assertRaises(RuntimeError):
                ermittle_image_id("legal:1", run=lambda a, timeout=0, rc=rc, out=out:
                                  subprocess.CompletedProcess(a, rc, out, b""))

    def test_credentials_vorhanden(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertFalse(credentials_vorhanden("claude", {}, tmp))
        self.assertTrue(credentials_vorhanden("claude", {"ANTHROPIC_API_KEY": "k"}, tmp))
        self.assertFalse(credentials_vorhanden("codex", {"ANTHROPIC_API_KEY": "k"}, tmp))
        (tmp / ".codex").mkdir()
        os.symlink(tmp / "x", tmp / ".codex" / "auth.json")
        self.assertFalse(credentials_vorhanden("codex", {}, tmp))
        (tmp / ".claude").mkdir()
        (tmp / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        self.assertTrue(credentials_vorhanden("claude", {}, tmp))


if __name__ == "__main__":
    unittest.main()
