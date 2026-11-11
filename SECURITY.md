# Security policy

## Reporting a vulnerability

Report a suspected vulnerability privately through GitHub's **Report a vulnerability** button on this repository's **Security** tab (private vulnerability reporting). Don't open a public issue or pull request for it.

Include what you found, how to reproduce it, the affected release tag or commit, and its impact. The maintainer acknowledges a report within 5 business days and coordinates a fix and disclosure with you.

## Supported versions

Security fixes go into the next release from `main`. Only the latest `vMAJOR.MINOR.PATCH` release is supported. Consumers should pin a released tag and update through `Tools/boundroom-sync.sh`.

## Scope

In scope: the supervisor, its policy and schemas, the sync tool, the plugin's skills and manifests, and this repository's workflows. Out of scope: consumer repositories' own code, and third-party services such as model providers.

How changes are controlled and evidenced is described in [docs/compliance/controls.md](docs/compliance/controls.md).
