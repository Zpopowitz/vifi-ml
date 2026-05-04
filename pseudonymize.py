"""Subject-id pseudonymization for HIPAA Safe Harbor compliance.

HIPAA's Safe Harbor de-identification (45 CFR 164.514(b)(2)) requires
removing 18 categories of identifiers including names, dates more
specific than year, geographic subdivisions smaller than state, and
"any other unique identifying number, characteristic, or code."

A subject id like `founder` or `patient_smith_001` is one of those
"unique codes." This module replaces it with a deterministic pseudonym
derived by HMAC-SHA256 over a server-held salt:

    pseudo = HMAC_SHA256(salt, subject_id)[:16]

Properties:
  * **Stable**: same subject_id always pseudonymizes to the same value,
    so longitudinal records can be joined without revealing identity.
  * **One-way**: the salt is held only by the deployment; without it
    you cannot brute-force a 16-char hex back to a real subject id
    (random keyspace makes the dictionary attack unfeasible).
  * **Per-deployment**: each deployment uses its own salt, so leaking
    one site's pseudonyms doesn't leak another's.

The mapping (real subject -> pseudonym) is intentionally NOT stored by
this module. Whoever owns the subject roster maintains that mapping in
a separate secured system (encrypted CSV, sealed envelope, dedicated
DB, etc.) -- the principle of separating identifiers from clinical
data is the whole point of pseudonymization.

Configuration:
    VIFI_PSEUDO_SALT      : the salt (any non-empty string >= 32 chars)
                            generate with: openssl rand -hex 32
                            REQUIRED in production. If unset and
                            VIFI_REQUIRE_PSEUDO=true, raises at first
                            call (fail-closed for prod). If unset and
                            VIFI_REQUIRE_PSEUDO=false (the default),
                            falls back to a clearly-marked
                            "pseudo-dev:<id>" string so dev runs work
                            without ceremony.
    VIFI_REQUIRE_PSEUDO   : "true" to require the salt. Default "false".

Usage:
    from pseudonymize import pseudonymize
    pseudo_id = pseudonymize("founder")  # -> "pseudo:7a2c..." (16 hex)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

log = logging.getLogger("vifi.pseudonymize")

_DEV_PREFIX = "pseudo-dev:"
_PROD_PREFIX = "pseudo:"

# Salt length floor. Anything shorter is too easy to brute-force.
_MIN_SALT_LEN = 32

_warned_no_salt = False


def _get_salt() -> Optional[str]:
    salt = os.environ.get("VIFI_PSEUDO_SALT")
    if not salt:
        return None
    if len(salt) < _MIN_SALT_LEN:
        raise RuntimeError(
            f"VIFI_PSEUDO_SALT is too short ({len(salt)} chars). "
            f"Use at least {_MIN_SALT_LEN}; generate with "
            f"`openssl rand -hex 32`."
        )
    return salt


def _require_salt() -> bool:
    return os.environ.get("VIFI_REQUIRE_PSEUDO",
                          "false").lower() == "true"


def pseudonymize(subject_id: Optional[str]) -> Optional[str]:
    """Return the deterministic pseudonym for `subject_id`.

    None pass-through (records that aren't tied to a subject -- e.g.
    health probes -- shouldn't have a pseudonym field at all).
    """
    if subject_id is None:
        return None
    if not isinstance(subject_id, str):
        raise TypeError(f"subject_id must be str, got {type(subject_id).__name__}")

    salt = _get_salt()
    if salt is None:
        if _require_salt():
            raise RuntimeError(
                "VIFI_REQUIRE_PSEUDO=true but VIFI_PSEUDO_SALT is not set. "
                "Refusing to write identifiable data. Set the salt or "
                "disable VIFI_REQUIRE_PSEUDO (dev only)."
            )
        global _warned_no_salt
        if not _warned_no_salt:
            log.warning(
                "VIFI_PSEUDO_SALT not set; subject ids written in clear "
                "as %r prefix. Do NOT use this configuration with real "
                "patient data.", _DEV_PREFIX
            )
            _warned_no_salt = True
        return f"{_DEV_PREFIX}{subject_id}"

    digest = hmac.new(salt.encode("utf-8"),
                      subject_id.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"{_PROD_PREFIX}{digest[:16]}"


def is_pseudonymous(value: Optional[str]) -> bool:
    """True if a string is in pseudonymous form.

    Useful for a tripwire test: every audit-log subject field should
    pass this check before being persisted.
    """
    return isinstance(value, str) and (
        value.startswith(_PROD_PREFIX) or value.startswith(_DEV_PREFIX)
    )
