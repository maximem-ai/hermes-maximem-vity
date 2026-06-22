"""hermes-maximem-vity — Vity (Maximem AI) memory plugin for Hermes Agent.

Installed from PyPI:

    pip install hermes-maximem-vity
    hermes-maximem-vity install      # copies plugin into ~/.hermes/plugins/vity/ + activates it
    echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env

This package ships the plugin files as a payload and the ``hermes-maximem-vity``
console command copies them into the Hermes plugins directory, where Hermes
auto-discovers the provider — then sets ``memory.provider: vity`` for you
(deterministically, avoiding the fragile interactive ``hermes memory setup``
wizard). The provider code itself imports Hermes host modules (``agent``,
``tools``) at runtime, so it is shipped as data and never imported by this
package directly.
"""

__version__ = "1.0.2"
