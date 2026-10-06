# ADR 0011: Harness versions are pinned per image, recorded per attempt, and promoted explicitly

Status: accepted, operator decision 16, 2026-09-16. Promotion and rollback are per
harness since ADR 0018 (2026-09-25).

## Context

Harness CLIs change flags, output shapes, and authentication behavior
between versions and can update themselves. An unpinned harness makes
every attempt's behavior unreproducible.

## Decision

- Each worker image pins one harness version, labels it, and disables the
  CLI's self-update; the read-only root filesystem makes an update
  impossible regardless.
- Every attempt records the image digest it ran. Retries and corrections
  keep that digest unless Foundry explicitly authorizes a change.
- Each adapter declares its tested version range; the API reports
  installed and supported versions and refuses unsupported combinations.
- Promotion: the weekly and on-demand `harness-pins.yml` workflow opens one
  update PR per harness, with the release changelog and adapter range result; CI builds a digest-pinned
  candidate; adapter contract tests run; a bounded live canary runs
  outside CI; a person reviews flags, output, auth, and parsing; the
  supported range is updated; the image publishes to GHCR on release; an
  admin promotes it to default explicitly; one prior known-good image is
  retained.

## Consequences

Worker images are versioned artifacts with their own release cadence.
GHCR publication waits for the live harness phase and the release
workflow. A harness that removes its auto-update opt-out is caught by
S11's re-run on each promotion.
