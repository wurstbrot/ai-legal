"""CLI-Einstieg `legal` (`legal.bash <fallordner> [--keep]`).

Exit-Codes: 0 final, 1 unerwarteter Fehler, 2 Eingabe/Konfiguration,
3 nicht final, 4 Hold/Budget/Laufzeit. Ausgaben liegen unter
`<fallordner>/ergebnis/<lauf-id>/` (Dateien 0600, Verzeichnisse 0700).
"""
from __future__ import annotations

import argparse
import datetime
import os
import secrets
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from .conductor import EXIT_EINGABE, EXIT_FEHLER, EXIT_STOPP, Conductor, Runner
from .config import FAMILIEN, KonfigFehler, lade_konfig
from .container import DockerRunner, Netzwerke, credentials_vorhanden, ermittle_image_id, installiere_signal_handler
from .eingabe import EingabeFehler, MountUnsicher, lies_anweisung, pruefe_fallordner
from .ergebnis import erstelle_verzeichnis
from .logging_setup import get_logger, progress, sec_event, setze_lauf_id, terminal_sicher


def neue_lauf_id(jetzt: datetime.datetime | None = None) -> str:
    """Lauf-ID: UTC-Zeitstempel plus Zufallsanteil (`secrets`)."""
    t = jetzt or datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="legal.bash", description="KI-Rechtsanwalt-Beratung (Entwurf für Timo).")
    p.add_argument("fallordner", help="Ordner mit anweisung.md und fall/ (nur .txt/.md, vorab pseudonymisiert)")
    p.add_argument("--keep", action="store_true", help="Arbeitsverzeichnisse nach dem Lauf behalten (E-9)")
    return p


def _ergebnis_basis(root: Path) -> Path:
    """`<fallordner>/ergebnis/` anlegen bzw. prüfen: echtes Verzeichnis, kein Symlink."""
    basis = root / "ergebnis"
    try:
        st = os.lstat(basis)
    except FileNotFoundError:
        return erstelle_verzeichnis(basis)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        sec_event("legal.input.rejected", "rejected", komponente="eingabe", reason="ergebnis_dir")
        raise EingabeFehler("ergebnis_dir")
    return basis


def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None,
         env: dict[str, str] | None = None, heute: datetime.date | None = None,
         uhr: Callable[[], float] | None = None, host_home: Path | None = None,
         image_id: str = "") -> int:
    """Einstieg; `runner` ist für Tests injizierbar (dann ohne Docker-Netze und ohne Image-Auflösung)."""
    get_logger()
    args = _parser().parse_args(argv)
    lauf_id = neue_lauf_id()
    setze_lauf_id(lauf_id)
    umgebung = dict(os.environ if env is None else env)
    try:
        konfig = lade_konfig(umgebung)
        pfad = Path(args.fallordner)
        if not pfad.is_absolute() and umgebung.get("LEGAL_AUFRUF_CWD"):
            pfad = Path(umgebung["LEGAL_AUFRUF_CWD"]) / pfad
        fall = pruefe_fallordner(pfad, konfig.mapping_namen)
        anweisung = lies_anweisung(fall.anweisung)
        if runner is None:
            # Preflight vor dem ersten Modellaufruf: Credentials je Familie (E-3) und Image-ID.
            heim = host_home or Path.home()
            fehlend = [f for f in FAMILIEN if not credentials_vorhanden(f, umgebung, heim)]
            if fehlend:
                for f in fehlend:
                    sec_event("legal.credentials.missing", "rejected", komponente="preflight", familie=f)
                progress("Credentials fehlen (API-Key oder Auth-Datei je Modellfamilie) – Abbruch")
                return EXIT_EINGABE
            try:
                image_id = ermittle_image_id(konfig.image)
            except RuntimeError:
                sec_event("legal.image.unresolved", "rejected", komponente="preflight")
                progress("Image nicht auflösbar (gebaut? docker verfügbar?) – Abbruch")
                return EXIT_EINGABE
        ergebnis_dir = erstelle_verzeichnis(_ergebnis_basis(fall.root) / lauf_id)
    except KonfigFehler:
        sec_event("legal.config.rejected", "rejected", komponente="config")
        progress("Konfiguration ungültig – Abbruch vor jedem Modellaufruf")
        return EXIT_EINGABE
    except (EingabeFehler, MountUnsicher):
        progress("Fallordner ungültig – Abbruch vor jedem Modellaufruf")
        return EXIT_EINGABE
    except OSError as exc:
        # z. B. fehlende Leserechte auf fall/ oder ergebnis/ nicht anlegbar; keine Pfade ins Log.
        sec_event("legal.input.rejected", "rejected", komponente="eingabe", reason="os_error",
                  error=type(exc).__name__)
        progress("Fallordner nicht lesbar oder Ergebnisordner nicht anlegbar – Abbruch vor jedem Modellaufruf")
        return EXIT_EINGABE

    arbeits_dir = Path(tempfile.mkdtemp(prefix="legal-"))   # mkdtemp legt 0700 an
    code = EXIT_FEHLER
    try:
        kwargs = {"konfig": konfig, "fall": fall, "anweisung": anweisung, "ergebnis_dir": ergebnis_dir,
                  "arbeits_dir": arbeits_dir, "lauf_id": lauf_id, "heute": heute, "image_id": image_id}
        if uhr is not None:
            kwargs["uhr"] = uhr
        if runner is not None:
            code = Conductor(runner=runner, **kwargs).lauf()
        else:
            installiere_signal_handler(lauf_id)
            # Alle Container laufen über die einmal aufgelöste, unveränderliche Image-ID.
            with Netzwerke(lauf_id, image=image_id, llm_domains=konfig.llm_domains,
                           research_domains=konfig.research_domains) as netze:
                code = Conductor(runner=DockerRunner(image=image_id, netze=netze, env=umgebung,
                                                     host_home=host_home), **kwargs).lauf()
    except KeyboardInterrupt:
        sec_event("legal.run.not_final", "aborted", reason="signal")
        code = EXIT_STOPP
    except Exception:  # noqa: BLE001 - keine Stacktraces nach außen
        sec_event("legal.run.not_final", "aborted", reason="unexpected")
        progress("Unerwarteter Fehler (z. B. Docker oder Image nicht verfügbar) – Lauf abgebrochen")
        code = EXIT_FEHLER
    finally:
        if args.keep:
            progress("Arbeitsverzeichnis behalten (--keep): " + str(arbeits_dir))
        else:
            shutil.rmtree(arbeits_dir, ignore_errors=True)
    print(terminal_sicher(f"Ergebnis: {ergebnis_dir} (Exit {code})"))
    return code


if __name__ == "__main__":
    sys.exit(main())
