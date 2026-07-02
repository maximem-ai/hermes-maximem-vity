"""``hermes-maximem-vity`` console script — install/uninstall the Vity plugin.

A plain ``pip install`` lands this package in site-packages, but Hermes
discovers memory providers from ``$HERMES_HOME/plugins/<name>/``. This command
bridges the gap: it copies the bundled plugin payload into
``~/.hermes/plugins/maximem_vity/`` so Hermes picks it up.

Usage:
    hermes-maximem-vity install      # copy plugin into ~/.hermes/plugins/maximem_vity/
    hermes-maximem-vity uninstall    # remove it
    hermes-maximem-vity status       # show install location + state
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_NAME = "maximem_vity"

# Pre-rename folder name. Removed on install so Hermes Desktop's Memory Provider
# list shows only "Maximem Vity", not a stale "Vity" alongside it.
_LEGACY_PLUGIN_NAME = "vity"

# payload filename -> installed filename in ~/.hermes/plugins/maximem_vity/
_FILES = {
    "provider.py": "__init__.py",   # the MemoryProvider Hermes loads
    "cli.py": "cli.py",             # the `hermes maximem_vity` subcommands
    "plugin.yaml": "plugin.yaml",   # manifest (deps, required env)
    "vity.json.example": "vity.json.example",
    "after-install.md": "after-install.md",
}


def _payload_dir() -> Path:
    return Path(__file__).resolve().parent / "payload"


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    # Match the host's platform default: %LOCALAPPDATA%\hermes on Windows,
    # ~/.hermes elsewhere. (get_hermes_home() in hermes_constants does the same.)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _target_dir() -> Path:
    return _hermes_home() / "plugins" / PLUGIN_NAME


def _env_path() -> Path:
    return _hermes_home() / ".env"


def _key_already_set() -> bool:
    """True if MAXIMEM_API_KEY (or VITY_API_KEY) is set in env or ~/.hermes/.env."""
    if os.environ.get("MAXIMEM_API_KEY") or os.environ.get("VITY_API_KEY"):
        return True
    env = _env_path()
    if env.exists():
        for line in env.read_text(errors="replace").splitlines():
            s = line.strip()
            if s.startswith(("MAXIMEM_API_KEY=", "VITY_API_KEY=")) and s.split("=", 1)[1].strip():
                return True
    return False


def _write_api_key(key: str) -> None:
    """Write MAXIMEM_API_KEY to ~/.hermes/.env, replacing any existing line.

    Dedupes — never appends a second MAXIMEM_API_KEY (the common copy-paste bug).
    """
    env = _env_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    lines = env.read_text(errors="replace").splitlines() if env.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith("MAXIMEM_API_KEY="):
            if not found:
                out.append(f"MAXIMEM_API_KEY={key}")
                found = True
            # drop any duplicate MAXIMEM_API_KEY lines
        else:
            out.append(line)
    if not found:
        out.append(f"MAXIMEM_API_KEY={key}")
    env.write_text("\n".join(out) + "\n")
    try:
        env.chmod(0o600)
    except Exception:
        pass


def _ensure_api_key(api_key: str | None) -> bool:
    """Make sure an API key is configured. Returns True if a key is now set.

    Priority: --api-key flag > already-set > interactive prompt (tty only).
    """
    if api_key:
        _write_api_key(api_key.strip())
        print(f"✓ Saved MAXIMEM_API_KEY to {_env_path()}")
        return True
    if _key_already_set():
        print("✓ MAXIMEM_API_KEY already configured (pass --api-key mx_… to change it)")
        return True
    # Prompt only on a real terminal — never hang on piped/buffered input.
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            entered = input("\nPaste your Maximem API key (mx_…) or press Enter to skip: ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        if entered:
            _write_api_key(entered)
            print(f"✓ Saved MAXIMEM_API_KEY to {_env_path()}")
            return True
    return False


def _venv_python(venv_dir: Path) -> Path:
    """Python interpreter inside a venv — Windows: ``Scripts\\python.exe``; else ``bin/python``."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _hermes_python() -> str | None:
    """Resolve the Python interpreter that the ``hermes`` runtime actually uses.

    The Vity plugin runs *inside* the Hermes process, which normally lives in
    its own isolated virtualenv (``<HERMES_HOME>/hermes-agent/venv``) — NOT the
    interpreter that ran ``pip install hermes-maximem-vity`` (often Anaconda base
    or the system Python). The SDK must be importable by *Hermes'* interpreter,
    so we resolve it here, cross-platform: Windows uses ``venv\\Scripts\\python.exe``,
    macOS/Linux use ``venv/bin/python``. Returns the interpreter path, or None.
    """
    candidates: list[Path] = []

    # 1) Standard Hermes layout: a dedicated venv under HERMES_HOME.
    candidates.append(_venv_python(_hermes_home() / "hermes-agent" / "venv"))

    # 2) Derive it from the `hermes` launcher on PATH.
    hermes = shutil.which("hermes")
    if hermes:
        launcher = Path(hermes)
        # 2a) launcher sits in the venv's bin/Scripts dir -> sibling python.
        candidates.append(launcher.with_name("python.exe" if os.name == "nt" else "python"))
        try:
            text = launcher.read_text(errors="replace")
        except Exception:
            text = ""
        lines = text.splitlines()
        # 2b) wrapper referencing a venv path (bin on Unix, Scripts on Windows).
        m = re.search(r'([^\s"\']+)[\\/](?:bin|Scripts)[\\/]hermes', text)
        if m:
            candidates.append(_venv_python(Path(m.group(1))))
        # 2c) Python console script (Unix): the shebang IS the interpreter.
        if lines and lines[0].startswith("#!") and "python" in lines[0]:
            candidates.append(Path(lines[0][2:].strip().split()[0]))

    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except OSError:
            continue
    return None


def _install_sdk_into_hermes() -> bool:
    """Install ``maximem-vity-sdk`` into the interpreter that runs Hermes.

    This is the step a plain ``pip install`` cannot do: the SDK is a dependency
    of *this* package, so it lands in whatever Python ran pip — but Hermes runs
    elsewhere. We target Hermes' own interpreter, bootstrapping pip via
    ``ensurepip`` if the venv was created without it. Best-effort: returns False
    (with a clear manual fallback printed by the caller) if it can't be done.
    """
    py = _hermes_python()
    if not py:
        return False

    # Already importable by Hermes? Nothing to do.
    if subprocess.run([py, "-c", "import maximem_vity"], capture_output=True).returncode == 0:
        return True

    print("  installing maximem-vity-sdk into Hermes' environment (one-time)…")

    # Make sure pip exists in that interpreter (venvs are often built without it).
    if subprocess.run([py, "-m", "pip", "--version"], capture_output=True).returncode != 0:
        subprocess.run([py, "-m", "ensurepip", "--upgrade"], capture_output=True, timeout=180)

    try:
        proc = subprocess.run(
            [py, "-m", "pip", "install", "maximem-vity-sdk>=0.2.1,<1"],
            capture_output=True, timeout=300,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False

    # Verify the import really works now (not just that pip exited 0).
    return subprocess.run([py, "-c", "import maximem_vity"], capture_output=True).returncode == 0


def _activate_provider() -> bool:
    """Set ``memory.provider = maximem_vity`` deterministically via the hermes CLI.

    The interactive ``hermes memory setup`` wizard is fragile (buffered/pasted
    input can silently drop the selection, leaving the provider at "none"), so
    we activate non-interactively here. Best-effort: returns False if the
    ``hermes`` CLI isn't on PATH or the call fails.
    """
    hermes = shutil.which("hermes")
    if not hermes:
        return False
    try:
        subprocess.run(
            [hermes, "config", "set", "memory.provider", PLUGIN_NAME],
            check=True, capture_output=True, timeout=60,
        )
        return True
    except Exception:
        return False


def cmd_install(force: bool = False, api_key: str | None = None) -> int:
    payload = _payload_dir()
    if not payload.is_dir():
        print(f"✗ payload not found at {payload} — broken install?", file=sys.stderr)
        return 1
    target = _target_dir()
    target.mkdir(parents=True, exist_ok=True)

    # Migrate away from the pre-rename folder so the Memory Provider list doesn't
    # show a stale "Vity" next to "Maximem Vity".
    legacy = _hermes_home() / "plugins" / _LEGACY_PLUGIN_NAME
    if legacy.exists() and legacy.resolve() != target.resolve():
        try:
            shutil.rmtree(legacy)
            print(f"✓ Removed legacy plugin folder {legacy}")
        except Exception:
            print(f"  (note: couldn't remove old {legacy} — delete it manually)")

    copied = 0
    for src_name, dst_name in _FILES.items():
        src = payload / src_name
        if src.exists():
            shutil.copy2(src, target / dst_name)
            copied += 1

    # Seed vity.json from the example on first install (non-secret tunables).
    example = target / "vity.json.example"
    config = target / "vity.json"
    if example.exists() and not config.exists():
        shutil.copy2(example, config)
    print(f"✓ Plugin files installed to {target} ({copied} files)")

    # 1) Install the SDK into Hermes' OWN interpreter. A plain `pip install`
    #    put the SDK in whatever Python ran it (often Anaconda/system), but
    #    Hermes runs in its own venv and imports `maximem_vity` there — without
    #    this step it reports "maximem-vity-sdk not installed" despite pip.
    sdk_ok = _install_sdk_into_hermes()
    if sdk_ok:
        print("✓ SDK available to Hermes (maximem-vity-sdk)")

    # 2) API key (flag > already-set > prompt) — written deduped to ~/.hermes/.env
    key_ok = _ensure_api_key(api_key)

    # 3) Activate non-interactively (avoids the fragile `hermes memory setup` wizard)
    activated = _activate_provider()
    if activated:
        print(f"✓ Activated: memory.provider = {PLUGIN_NAME}")

    # 4) Clear, honest summary — no guesswork for the user
    print("\n" + "─" * 52)
    if key_ok and activated and sdk_ok:
        print("✅ All set! Vity memory is active.")
        print("   Start Hermes:   hermes")
        print("   Check it:       hermes maximem_vity status")
    else:
        print("Almost done — finish these:")
        if not sdk_ok:
            hpy = _hermes_python()
            target_py = hpy or "<your hermes venv python>"
            print("  • Install the SDK into Hermes' interpreter:")
            print(f"      {target_py} -m pip install 'maximem-vity-sdk>=0.2.1,<1'")
        if not key_ok:
            print("  • Add your API key:")
            print("      echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env")
            print("      (get one at https://app.maximem.ai/api-keys)")
        if not activated:
            print("  • Activate the provider (hermes wasn't on PATH):")
            print(f"      hermes config set memory.provider {PLUGIN_NAME}")
            print("      (not the interactive `hermes memory setup` — it can drop the selection)")
        print("  Then:  hermes maximem_vity status")
    print("─" * 52)
    return 0


def cmd_uninstall() -> int:
    target = _target_dir()
    if target.exists():
        shutil.rmtree(target)
        print(f"✓ Removed {target}")
    else:
        print("Vity plugin is not installed.")
    return 0


def cmd_status() -> int:
    target = _target_dir()
    installed = (target / "__init__.py").exists()
    print(f"Vity plugin: {'installed ✓' if installed else 'not installed ✗'}")
    print(f"  location: {target}")
    print(f"  payload:  {_payload_dir()}")

    # The exact thing that broke before: can Hermes' interpreter import the SDK?
    py = _hermes_python()
    if not py:
        print("  SDK:      hermes interpreter not found (run `hermes` once?)")
    else:
        importable = subprocess.run(
            [py, "-c", "import maximem_vity"], capture_output=True
        ).returncode == 0
        print(f"  SDK:      {'available to Hermes ✓' if importable else 'NOT importable by Hermes ✗ — run: hermes-maximem-vity install'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-maximem-vity",
        description="Install the Vity (Maximem AI) memory plugin into Hermes.",
    )
    sub = parser.add_subparsers(dest="cmd")
    p_install = sub.add_parser("install", help="Install + activate the plugin (one command)")
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing install")
    p_install.add_argument("--api-key", metavar="mx_…", default=None,
                           help="Maximem API key — saved to ~/.hermes/.env (deduped)")
    sub.add_parser("uninstall", help="Remove the plugin from ~/.hermes/plugins/maximem_vity/")
    sub.add_parser("status", help="Show install location and state")

    args = parser.parse_args(argv)
    if args.cmd == "install":
        return cmd_install(force=getattr(args, "force", False), api_key=getattr(args, "api_key", None))
    if args.cmd == "uninstall":
        return cmd_uninstall()
    if args.cmd == "status":
        return cmd_status()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
