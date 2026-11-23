# Canonical quality contract for the reusable harness.
#
# `reusable-check` is the single named gate a consumer's sync preflight invokes before
# importing this repository. It owns which tools run; the sync tool only asks whether the
# gate passed, so adding a check here needs no change on the consumer side.

UV ?= uv

.PHONY: reusable-check demo-reject

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
