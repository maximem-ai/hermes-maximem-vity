"""``hermes-maximem-vity`` console script — install/uninstall the Vity plugin.

A plain ``pip install`` lands this package in site-packages, but Hermes
discovers memory providers from ``$HERMES_HOME/plugins/<name>/``. This command
bridges the gap: it copies the bundled plugin payload into
``~/.hermes/plugins/vity/`` so Hermes picks it up.

Usage:
    hermes-maximem-vity install      # copy plugin into ~/.hermes/plugins/vity/
    hermes-maximem-vity uninstall    # remove it
    hermes-maximem-vity status       # show install location + state
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_NAME = "vity"

# payload filename -> installed filename in ~/.hermes/plugins/vity/
_FILES = {
    "provider.py": "__init__.py",   # the MemoryProvider Hermes loads
    "cli.py": "cli.py",             # the `hermes vity` subcommands
    "plugin.yaml": "plugin.yaml",   # manifest (deps, required env)
    "vity.json.example": "vity.json.example",
    "after-install.md": "after-install.md",
}


def _payload_dir() -> Path:
    return Path(__file__).resolve().parent / "payload"


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


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
        print("✓ Saved MAXIMEM_API_KEY to ~/.hermes/.env")
        return True
    if _key_already_set():
        print("✓ MAXIMEM_API_KEY already configured")
        return True
    # Prompt only on a real terminal — never hang on piped/buffered input.
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            entered = input("\nPaste your Maximem API key (mx_…) or press Enter to skip: ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        if entered:
            _write_api_key(entered)
            print("✓ Saved MAXIMEM_API_KEY to ~/.hermes/.env")
            return True
    return False


def _activate_provider() -> bool:
    """Set ``memory.provider = vity`` deterministically via the hermes CLI.

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
            [hermes, "config", "set", "memory.provider", "vity"],
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

    # 1) API key (flag > already-set > prompt) — written deduped to ~/.hermes/.env
    key_ok = _ensure_api_key(api_key)

    # 2) Activate non-interactively (avoids the fragile `hermes memory setup` wizard)
    activated = _activate_provider()
    if activated:
        print("✓ Activated: memory.provider = vity")

    # 3) Clear, honest summary — no guesswork for the user
    print("\n" + "─" * 52)
    if key_ok and activated:
        print("✅ All set! Vity memory is active.")
        print("   Start Hermes:   hermes")
        print("   Check it:       hermes vity status")
    else:
        print("Almost done — finish these:")
        if not key_ok:
            print("  • Add your API key:")
            print("      echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env")
            print("      (get one at https://app.maximem.ai/api-keys)")
        if not activated:
            print("  • Activate the provider (hermes wasn't on PATH):")
            print("      hermes config set memory.provider vity")
        print("  Then:  hermes vity status")
    print("─" * 52)
    print("Tip: do NOT use the interactive `hermes memory setup` to activate —")
    print("     `hermes config set memory.provider vity` is the reliable way.")
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
    sub.add_parser("uninstall", help="Remove the plugin from ~/.hermes/plugins/vity/")
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
