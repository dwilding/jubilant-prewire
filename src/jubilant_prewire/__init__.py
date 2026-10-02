"""jubilant-prewire: pre-pull OCI images for K8s charm deploys in pytest.

A pytest plugin that intercepts ``jubilant.Juju.deploy`` calls, pulls the
charm's OCI image into containerd before the deploy proceeds, then lets
jubilant do its work as normal. See DESIGN.md for details.
"""

from ._version import __version__

__all__ = ["__version__"]
