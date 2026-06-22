"""hermes-maximem-vity — Vity (Maximem AI) memory plugin for Hermes Agent.

Installed from PyPI:

    pip install hermes-maximem-vity
    hermes-maximem-vity install      # drops the plugin into ~/.hermes/plugins/vity/
    hermes memory setup vity

This package ships the plugin files as a payload and the ``hermes-maximem-vity``
console command copies them into the Hermes plugins directory, where Hermes
auto-discovers the provider. The provider code itself imports Hermes host
modules (``agent``, ``tools``) at runtime, so it is shipped as data and never
imported by this package directly.
"""

__version__ = "1.0.0"
