# Randomized Exercise Soak Catalogue

`catalog-v1.yml` is the version 1 catalogue index. Each entry points to an immutable-revision definition under `epochs/` and records its canonical SHA-256 digest.

Validate the complete catalogue without starting Docker, subprocess nodes, ports, or OCI resources:

```bash
poetry run shardctl soak capabilities
poetry run shardctl soak validate --catalog soak/catalog-v1.yml --expected-schema 1
```

When reviewing a catalogue update, supply the previously pinned catalogue to enforce permanent IDs and semantic revision rules:

```bash
poetry run shardctl soak validate \
  --catalog soak/catalog-v1.yml \
  --previous-catalog /path/to/previous/catalog-v1.yml
```

Pin one identity during cross-repository compatibility checks:

```bash
poetry run shardctl soak validate \
  --catalog soak/catalog-v1.yml \
  --expected-schema 1 \
  --epoch SOAK-EPOCH-001 \
  --revision 1 \
  --definition-digest acfdf7fa5c9a2b9b90f95ff9598877d9e619d6bb77f67f0a7c733f2205d1d404
```

Files under `schemas/` are authoritative Draft 2020-12 inputs loaded by the validator; custom checks add path containment, normalized fixture uniqueness, digest, and catalogue-transition guarantees. Files under `fixtures/digest/` are normative canonical-byte and digest fixtures for independent implementations. Definitions marked `planned` are schema-compatible but are not yet executable; workload implementation tasks promote them to `implemented` without changing their semantic definition.

The normative interoperability rules are in [`docs/specs/randomized-exercise-soak-contract.md`](../docs/specs/randomized-exercise-soak-contract.md).
