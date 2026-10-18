# Wizard Connection Router

Slice 1: a Python standard-library service that discovers installed app needs,
records explicit owner model choices, and forwards isolated inference to provider
apps. Source and fake-provider tests only; no deployment or published image.

## Delivery contract

- Discover model and service requirements from an isolated `apps.json` fixture.
- Match exact tested models; require owner approval for substitutions; never
  auto-switch a recorded route; keep private features on private providers.
- Exercise real loopback HTTP and SSE, grants, persisted choices, and fail-closed
  effective-model verification against fake providers.
- Ship a reviewable source PR, not a live installation.

Implementation: direct stdlib Python. Consequential boundary: model provenance,
private data routing, and owner-vs-app authority. Independent promotion review
belongs to the parent lane before any real-provider deployment.
