<!-- Thanks for contributing to ViFi! Fill in the fields below. -->

## Summary

<!-- 1-3 sentences explaining what this PR does and why. -->

## Linked plan / issue

<!-- e.g., implements I042 + I044 from /root/.claude/plans/... -->

## Type of change

- [ ] Bug fix (no API change)
- [ ] New feature (additive only)
- [ ] Breaking change (API or audit-log schema)
- [ ] Documentation only
- [ ] Refactor (no behavior change)
- [ ] Test or CI

## Compliance + safety review

- [ ] No new PHI fields added without pseudonymization (§5.I074).
- [ ] Audit log schema unchanged, OR `feature_set_version` bumped + migration documented.
- [ ] No new public API endpoints OR `PUBLIC_PATHS` updated with justification.
- [ ] Security headers + auth controls untouched OR explicitly reviewed.
- [ ] No secrets in code, tests, or docs (gitleaks pre-commit catches this).
- [ ] No telemetry that hits external services without explicit consent.

## Testing

- [ ] `make check` passes locally
- [ ] New behavior has tests
- [ ] Manual verification (describe below if applicable)

<!-- Manual verification steps: -->

## Documentation

- [ ] CHANGELOG.md updated under [Unreleased]
- [ ] README/SECURITY/COMPLIANCE updated if relevant

## Deployment notes

<!-- Anything an operator needs to do beyond `git pull`? Env var changes,
     migrations, build args? -->
