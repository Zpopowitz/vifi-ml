# Vulture whitelist: known false positives in the deadcode gauntlet
# (`vulture . --min-confidence 80 --exclude
# .venv,data,models,models_real,build,hr_net .vulture_whitelist.py`,
# see CLAUDE.md "Health Stack"). Each bare name below counts as a use,
# silencing the matching "unused" report. Keep entries commented with
# WHY they are false positives; delete an entry when the code it covers
# goes away. Never executed or imported; parsed by vulture and ruff only.
# ruff: noqa: F821, B018

# Context-manager __exit__ signature (audit.py:410): the protocol fixes
# the parameter names whether or not the body uses them.
exc_type
tb

# 501-stub signatures: parameters exist to pin the future API contract.
subject_signal  # modules/four_node_sync.py:60
n_sigma  # modules/transient_events.py:58,63
vitals_stream  # modules/transient_events.py:63

# Pytest fixtures requested for their side effects only: the fixture
# argument IS the dependency declaration.
compose_stack  # tests/test_compose_e2e.py:133,147,158
security  # tests/test_security.py:27 (import installs the auth middleware)

# Inner-class context-manager methods use `self_inner` to avoid
# shadowing the enclosing `self` (tests/test_worker_metrics.py).
self_inner
