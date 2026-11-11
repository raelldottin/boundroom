# Boundroom

Reusable supervised-slice automation distribution. This repository is the
publication target for the manifest-owned slice harness in
[owlory/owlory](https://github.com/owlory/owlory); changes flow one
way from Owlory into this repo via `Tools/boundroom-sync.sh`.

## Architecture and roadmap

- [SPEC-001: Kanban, self-hosted Hindsight and Swarm Wiki](architecture/control-plane.md) — proposed top-layer authority, integration boundaries and acceptance gates.
- [Product roadmap](product-roadmap.md) — current product vision, shipped-state audit, milestone horizons, priorities, success metrics and acceptance gates.
- [Technical roadmap (October 2026)](roadmap.md) — the original RA-CP implementation sequence and its historical acceptance contracts; benchmark/effectiveness work continues in parallel under explicit owner authorization.

The current JSON queue and supervisor behavior remain operational until an explicitly reviewed Kanban cutover. The Swarm Wiki compiler now supports Hindsight snapshots in pinned, synthetic tests, but live end-to-end publication and local-inference acceptance remain outstanding; see the [control-plane gates](architecture/control-plane.md). The repo ships no live fleet board, Hindsight datastore or wiki datastore.

## What lives here

- `automation/supervisor/` — queue selection, policy checks, validation
  ownership, diff-budget checks, and fresh-run launching.
- `automation/context/build_context.py` — compact slice context bundle
  generation.
- `automation/prompts/` — base, slice, and review prompt fragments
  (consumer-customizable).
- `automation/schemas/` — Pydantic contract models plus the JSON Schemas generated from them.
- `automation/examples/` — starter queue and handoff payloads for new
  repositories.
- `automation/tests/test_harness.py` — core supervisor and context-builder
  regression coverage.
- `Tools/boundroom-sync.sh` — manifest-owned sync between Owlory and
  this distribution target.
- `docs/workflows/boundroom.md` — full workflow contract including
  consumer adoption bootstrap.

## Consumer adoption

See [Workflow — Boundroom](workflows/boundroom.md) for the full
adoption sequence, known failure modes, and manual steps that remain for a
real consumer.

## Reporting issues

Bug reports and adoption questions go to
[github.com/raelldottin/boundroom/issues](https://github.com/raelldottin/boundroom/issues).
