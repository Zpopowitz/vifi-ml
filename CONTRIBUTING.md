# Contributing to ViFi

Thanks for considering a contribution. ViFi is research-grade; we
welcome bug fixes, doc improvements, and well-scoped features.

## Development setup

```bash
git clone https://github.com/Zpopowitz/vifi-ml.git
cd vifi-ml

# Recommended: virtual env
python3 -m venv .venv
source .venv/bin/activate

# Install everything (prod + dev deps)
make install-dev

# Verify
make check
```

## Test locally

```bash
make test            # full pytest
make lint            # ruff
make type            # mypy on strict-typed modules
make security-scan   # pip-audit + bandit
make sbom            # CycloneDX SBOM (committed in `sbom/`)
```

Or all together:

```bash
make check
```

## Branching

Trunk-based off `main`:

- `feat/<description>` — new features
- `fix/<description>` — bug fixes
- `chore/<description>` — refactors, deps, infra
- `docs/<description>` — docs only
- `exp/<description>` — experiments not intended to merge

## Commit messages

Conventional commits format:

```
<type>(<scope>): <subject>

<body — what changed and why; mention IDs from
/root/.claude/plans/i-want-you-to-warm-gizmo.md if applicable>
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `build`,
`ci`.

## Pull requests

- Fill out `.github/pull_request_template.md` completely.
- Run `make check` before opening.
- One logical change per PR (security review is hard with mixed bags).
- Update `CHANGELOG.md` under `[Unreleased]`.

## Code style

- ruff is the source of truth (`pyproject.toml`).
- Type hints on new code; mypy strict on `security.py`,
  `pseudonymize.py`, `audit.py`, `config.py`, `__version__.py`.
- No emojis in code unless requested.
- Don't write speculative comments. Code already says WHAT — only
  comment for non-obvious WHY.

## Adding a new bus topic

1. Add the topic helper to `modules/bus.py`.
2. Document it in `docs/DATA_DICTIONARY.md`.
3. Add a Pydantic schema for validation (planned, see ROADMAP).
4. Add a test that publishes + reads it via `InMemoryBus`.

## Adding a new endpoint

1. Define the request + response Pydantic models in `api.py`.
2. Add the handler — protected by default (auth required); only add
   to `PUBLIC_PATHS` in `security.py` if it's a probe with no PHI.
3. Add a test in `tests/test_api*.py`.
4. Add an OpenAPI tag if the endpoint groups with others.
5. Update `docs/API.md` (if it exists yet).

## Touching `audit.py`

Audit log is the most sensitive surface. Any change MUST:

- Preserve append-only semantics
- Preserve the chain-digest invariant (verify-chain test in
  `tests/test_audit_chain.py` must still pass)
- Update `docs/DATA_DICTIONARY.md` if you add/remove fields
- Get a CODEOWNERS review

## Reporting a security issue

Email `security@vifi.example` (placeholder). See `SECURITY.md`.

## License

By submitting code you agree it's licensed under the same terms as
this repo (see `LICENSE`).
