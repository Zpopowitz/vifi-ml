"""Single source of truth for the ViFi version string.

Embedded in /health responses and audit log entries so an operator
can identify exactly which build produced any given artifact.

Bump rules (semver):
- MAJOR: incompatible API or audit-log schema change
- MINOR: new endpoints, new bus topics, new model versions
- PATCH: bug fixes, doc updates, internal refactors
"""

from __future__ import annotations

__version__ = "0.4.0"
