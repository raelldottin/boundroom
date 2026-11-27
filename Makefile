# Canonical quality contract for the reusable harness.
#
# `reusable-check` is the single named gate a consumer's sync preflight invokes before
# importing this repository. It owns which tools run; the sync tool only asks whether the
# gate passed, so adding a check here needs no change on the consumer side.

UV ?= uv

.PHONY: reusable-check demo-reject mutation

reusable-check:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run ty check automation Tools
	$(UV) run python -m automation.schemas.generate --check
	$(UV) run python automation/supervisor/settings_view.py --docs --check
	$(UV) run python -m pytest automation/tests -q

# A self-contained demo, no agent or API key: the supervisor rejects a handoff whose claims the
# git diff contradicts. It works in a temporary repository and leaves this checkout untouched.
demo-reject:
	$(UV) run python automation/examples/demo_reject.py

# Mutation testing (docs/reference/mutation-testing.md): how many deliberately planted bugs in
# each module the tests catch. Not part of the gate; it reruns tests once per mutant and can take
# from minutes to hours. Fails when a score drops below quality/mutation-baseline.json.
#   make mutation MODULE=automation/supervisor/policy.py[,<path>...]
#   make mutation MODULE=... MUTATION_ARGS="--jobs 4"
# It runs at lowered priority (--nice); MUTATION_ARGS adds options.
MUTATION_ARGS ?=

mutation:
	@test -n "$(MODULE)" || { echo "usage: make mutation MODULE=<path>[,<path>...]" >&2; exit 64; }
	$(UV) run python -m Tools.mutate --check-baseline --nice $(MUTATION_ARGS) -- "$(MODULE)"
