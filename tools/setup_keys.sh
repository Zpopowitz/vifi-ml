#!/usr/bin/env bash
# setup_keys.sh — generate every secret ViFi needs in one command.
#
# Writes a `.env` file with freshly-generated:
#   VIFI_API_KEYS                — one API key, prefixed `vifi_`
#   VIFI_PSEUDO_SALT             — HMAC salt for HIPAA pseudonymization
#   VIFI_AUDIT_ENCRYPTION_KEY    — Fernet key for audit log at-rest
#   VIFI_AUDIT_CHAIN_KEY         — HMAC key for audit chain integrity
#   VIFI_REDIS_PASSWORD          — Redis requirepass (for non-localhost)
#   VIFI_BUS_URL_INTERNAL        — derived from the password above
#
# IDEMPOTENT: never overwrites an existing `.env` without explicit
# `--force`. Operators run this once on first deploy; subsequent
# rotations follow the per-key procedure in `docs/RUNBOOK.md`.
#
# Usage:
#     ./tools/setup_keys.sh                   # create .env if missing
#     ./tools/setup_keys.sh --force           # overwrite (lose old keys!)
#     ./tools/setup_keys.sh --print           # print without writing
#     ./tools/setup_keys.sh --auth-mode none  # dev mode (auth disabled)
#
# Why a script (not just docs):
#   1. One copy/paste mistake on a key generation = unrecoverable
#      audit log (Fernet) or orphaned pseudonyms (salt). The script
#      removes that hand-off.
#   2. Operators see one transcript instead of five `openssl` calls.
#   3. The script CAN be run again to rotate a single key (with
#      `--rotate <KEY_NAME>`), making the rotation procedure
#      auditable + reversible.

set -euo pipefail

# --- Defaults ---------------------------------------------------------------

ENV_FILE=".env"
FORCE=false
PRINT_ONLY=false
AUTH_MODE="api_key"     # "api_key" or "none"
ROTATE_KEY=""           # name of single key to rotate, leaving rest

# --- Argument parsing -------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)         FORCE=true; shift ;;
        --print)         PRINT_ONLY=true; shift ;;
        --auth-mode)     AUTH_MODE="$2"; shift 2 ;;
        --rotate)        ROTATE_KEY="$2"; shift 2 ;;
        --env-file)      ENV_FILE="$2"; shift 2 ;;
        -h|--help)
            sed -n '/^#/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "error: unknown argument $1" >&2
            echo "       run with --help for usage" >&2
            exit 2
            ;;
    esac
done

# --- Helpers ----------------------------------------------------------------

# 32 hex chars (128 bits) — enough for HMAC keys, salts, passwords.
gen_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "${1:-32}"
    else
        # Fall back to /dev/urandom directly — works on minimal images
        # (Alpine, distroless, Pi Zero with stripped openssl).
        local n="${1:-32}"
        head -c $((n)) /dev/urandom | xxd -p -c 256 | tr -d '\n' | cut -c1-$((n * 2))
    fi
}

# Fernet key: 32 random bytes, urlsafe-base64. Spec: cryptography.fernet.Fernet.generate_key.
gen_fernet() {
    python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
        2>/dev/null || {
        echo "error: python3 + cryptography needed for Fernet keygen" >&2
        echo "       try: pip install cryptography" >&2
        exit 3
    }
}

# Vifi-prefixed API key, ~43 hex chars after prefix.
gen_api_key() {
    if [[ -d "$(dirname "$0")/.." && -f "$(dirname "$0")/../security.py" ]]; then
        python3 -c 'from security import generate_api_key; print(generate_api_key())' \
            2>/dev/null && return 0
    fi
    # Fallback: generate-locally without needing the repo to be importable.
    echo "vifi_$(gen_hex 32)"
}

write_env_line() {
    local key="$1" value="$2"
    if $PRINT_ONLY; then
        printf '%s=%s\n' "$key" "$value"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

# --- Sanity checks ----------------------------------------------------------

if [[ "$AUTH_MODE" != "api_key" && "$AUTH_MODE" != "none" ]]; then
    echo "error: --auth-mode must be 'api_key' or 'none', got: $AUTH_MODE" >&2
    exit 2
fi

if [[ -n "$ROTATE_KEY" ]]; then
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "error: --rotate requires an existing $ENV_FILE" >&2
        exit 2
    fi
    case "$ROTATE_KEY" in
        VIFI_API_KEYS|VIFI_PSEUDO_SALT|VIFI_AUDIT_ENCRYPTION_KEY|\
        VIFI_AUDIT_CHAIN_KEY|VIFI_REDIS_PASSWORD)
            ;;
        *)
            echo "error: --rotate $ROTATE_KEY is not a known key" >&2
            echo "       valid: VIFI_API_KEYS, VIFI_PSEUDO_SALT," >&2
            echo "              VIFI_AUDIT_ENCRYPTION_KEY," >&2
            echo "              VIFI_AUDIT_CHAIN_KEY, VIFI_REDIS_PASSWORD" >&2
            exit 2
            ;;
    esac
fi

# --- Single-key rotation ----------------------------------------------------

if [[ -n "$ROTATE_KEY" ]]; then
    case "$ROTATE_KEY" in
        VIFI_API_KEYS)              new_value="$(gen_api_key)" ;;
        VIFI_PSEUDO_SALT)           new_value="$(gen_hex 32)" ;;
        VIFI_AUDIT_ENCRYPTION_KEY)  new_value="$(gen_fernet)" ;;
        VIFI_AUDIT_CHAIN_KEY)       new_value="$(gen_hex 32)" ;;
        VIFI_REDIS_PASSWORD)        new_value="$(gen_hex 32)" ;;
    esac

    if $PRINT_ONLY; then
        echo "$ROTATE_KEY=$new_value"
        exit 0
    fi

    # In-place edit (BSD + GNU sed compatible).
    if grep -q "^$ROTATE_KEY=" "$ENV_FILE"; then
        # macOS sed needs '' after -i; Linux doesn't. Detect at runtime.
        if sed --version >/dev/null 2>&1; then
            sed -i "s|^$ROTATE_KEY=.*$|$ROTATE_KEY=$new_value|" "$ENV_FILE"
        else
            sed -i '' "s|^$ROTATE_KEY=.*$|$ROTATE_KEY=$new_value|" "$ENV_FILE"
        fi
    else
        echo "$ROTATE_KEY=$new_value" >> "$ENV_FILE"
    fi

    echo "rotated $ROTATE_KEY in $ENV_FILE"
    echo
    echo "WARNING: rotating $ROTATE_KEY may invalidate existing data:"
    case "$ROTATE_KEY" in
        VIFI_PSEUDO_SALT)
            echo "  Audit logs created with the OLD salt are no longer joinable"
            echo "  with new logs. Plan a migration before rotating."
            ;;
        VIFI_AUDIT_ENCRYPTION_KEY)
            echo "  Audit records written with the OLD key cannot be decrypted"
            echo "  with the new one. Keep the old key in a sealed envelope"
            echo "  for 6 years (HIPAA retention)."
            ;;
        VIFI_AUDIT_CHAIN_KEY)
            echo "  audit_verify.py won't validate records signed with the OLD"
            echo "  key. Keep the old key for replay verification."
            ;;
        VIFI_REDIS_PASSWORD)
            echo "  Edge boxes still using the OLD password lose connectivity."
            echo "  Update each edge box's VIFI_BUS_URL_INTERNAL before"
            echo "  restarting the central server."
            ;;
        VIFI_API_KEYS)
            echo "  Existing dashboard sessions get logged out."
            echo "  Update operators with the new key before restarting."
            ;;
    esac
    exit 0
fi

# --- Full first-time setup --------------------------------------------------

if ! $PRINT_ONLY && [[ -f "$ENV_FILE" ]] && ! $FORCE; then
    echo "error: $ENV_FILE already exists." >&2
    echo "       To overwrite (DESTROYS CURRENT KEYS):" >&2
    echo "         $0 --force" >&2
    echo "       To rotate one key:" >&2
    echo "         $0 --rotate VIFI_PSEUDO_SALT" >&2
    echo "       To preview without writing:" >&2
    echo "         $0 --print" >&2
    exit 1
fi

# Generate everything before we touch the file (atomic-ish).
api_key="$(gen_api_key)"
pseudo_salt="$(gen_hex 32)"
audit_enc_key="$(gen_fernet)"
audit_chain_key="$(gen_hex 32)"
redis_password="$(gen_hex 32)"

# Build the .env content.
if $PRINT_ONLY; then
    target="/dev/stdout"
else
    target="$ENV_FILE"
    : > "$target"   # truncate
fi

# Header — important context for whoever opens this file later.
{
    echo "# Generated by tools/setup_keys.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# DO NOT COMMIT. Back up the contents to your secrets manager."
    echo "#"
    echo "# Recovery notes:"
    echo "#   * Losing VIFI_AUDIT_ENCRYPTION_KEY makes encrypted audit"
    echo "#     records permanently unreadable."
    echo "#   * Losing VIFI_PSEUDO_SALT orphans every audit subject ID"
    echo "#     written under it (no way to map back to real subject)."
    echo "#   * The other keys can be regenerated; affected components"
    echo "#     just need to be restarted with the new value."
    echo
} >> "$target"

# --- Auth + identity --------------------------------------------------------

write_env_line "VIFI_PATIENT_ID" "default"
write_env_line "VIFI_AUTH_MODE" "$AUTH_MODE"
if [[ "$AUTH_MODE" == "api_key" ]]; then
    write_env_line "VIFI_API_KEYS" "$api_key"
fi
write_env_line "VIFI_API_KEYS_FILE" ""
write_env_line "VIFI_CORS_ORIGINS" ""
write_env_line "VIFI_RATE_LIMIT" "60/minute"
write_env_line "VIFI_REVEAL_ERRORS" "false"
write_env_line "VIFI_TRUSTED_PROXIES" ""

# --- Privacy ----------------------------------------------------------------

write_env_line "VIFI_PSEUDO_SALT" "$pseudo_salt"
write_env_line "VIFI_REQUIRE_PSEUDO" "true"

# --- Audit ------------------------------------------------------------------

write_env_line "VIFI_AUDIT_ENCRYPTION_KEY" "$audit_enc_key"
write_env_line "VIFI_AUDIT_CHAIN_KEY" "$audit_chain_key"
write_env_line "VIFI_AUDIT_FSYNC" "true"
write_env_line "VIFI_AUDIT_DIR" "/app/data/audit"

# --- Redis / bus ------------------------------------------------------------

write_env_line "VIFI_REDIS_PASSWORD" "$redis_password"
write_env_line "VIFI_BUS_URL_INTERNAL" "redis://:$redis_password@redis:6379/0"
write_env_line "VIFI_BUS_URL" "redis://:$redis_password@localhost:6379/0"
write_env_line "VIFI_REDIS_PORT" "6379"

# --- Public-facing TLS ------------------------------------------------------

write_env_line "VIFI_DOMAIN" "localhost"

# --- Done -------------------------------------------------------------------

if $PRINT_ONLY; then
    exit 0
fi

# Make .env readable only by the owner — prevents `cat .env` by other users.
chmod 600 "$ENV_FILE"

cat <<EOF

✓ Wrote $ENV_FILE with fresh secrets (chmod 600).

Next steps:

  1. Back up the contents of $ENV_FILE to your secrets manager
     (1Password, AWS Secrets Manager, etc.). Losing some of these
     keys is unrecoverable — see notes at the top of the file.

  2. Save the API key separately for the dashboard:

         $api_key

  3. Bring up the stack:

         docker compose --profile dev up -d --build

  4. Open http://localhost:8501 and use the API key when prompted.

  5. For each edge box that connects to this central server, copy
     the relevant variables (VIFI_PSEUDO_SALT, VIFI_REDIS_PASSWORD,
     VIFI_BUS_URL_INTERNAL pointed at this central's IP).

EOF
