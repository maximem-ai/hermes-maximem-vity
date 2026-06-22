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


def cmd_install(force: bool = False) -> int:
    payload = _payload_dir()
    if not payload.is_dir():
        print(f"✗ payload not found at {payload} — broken install?", file=sys.stderr)
        return 1
    target = _target_dir()
    if target.exists() and not force:
        print(f"Vity plugin already installed at {target}. Re-run with --force to overwrite.")
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

    print(f"✓ Vity plugin installed to {target} ({copied} files)")
    print("\nNext steps:")
    print("  1. Set your key:   echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env")
    print("  2. Activate:       hermes memory setup vity")
    print("  3. Verify:         hermes vity status")
    print("\nGet an API key at https://app.maximem.ai/api-keys")
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
    p_install = sub.add_parser("install", help="Copy the plugin into ~/.hermes/plugins/vity/")
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing install")
    sub.add_parser("uninstall", help="Remove the plugin from ~/.hermes/plugins/vity/")
    sub.add_parser("status", help="Show install location and state")

    args = parser.parse_args(argv)
    if args.cmd == "install":
        return cmd_install(force=getattr(args, "force", False))
    if args.cmd == "uninstall":
        return cmd_uninstall()
    if args.cmd == "status":
        return cmd_status()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
