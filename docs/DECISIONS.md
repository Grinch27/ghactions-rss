# Current architecture decisions — 2026-10-28

- The public repository is an execution node. Registry, generated XML, state and gateway source are private.
- Every repository uses `main` only. Normal fast-forward pushes and bounded fetch/rebase preserve concurrent gateway registrations. No force push.
- Registration is passive. The four native GitHub schedules use Asia/Shanghai; no per-route dispatch or Pages rebuild.
- RSS and Atom are public. Public snapshots contain hashed file paths and no registry/index listing. This does not make published feed content confidential.
- Exact path, query order, duplicate parameters and percent encoding are retained in subscription identity. Only native RSS/Atom outputs are supported.
- Use the official full image with bundled Chromium; upstream route implementations are not copied. Provider credentials can be supplied by `RSSHUB_ENV`.
- Different providers may refresh concurrently; requests to the same provider are serialized with a minimum interval. No automatic feed retry.
- Failed refreshes retain valid previous XML. An invalid baseline that cannot be replaced blocks the complete publication. A new unavailable feed remains pending until a later successful refresh.
- Results are committed privately before Pages Direct Upload. Registration commits do not trigger Pages. This needs no Deploy Hook secret or private GitHub App connection in Pages.
- Deploy with `cloudflare/wrangler-action@v4`, without a Wrangler version pin. Raw logs, responses, XML and detailed reports are not public artifacts.
- A first registration returns a valid empty pending feed only after GitHub acknowledges the record. New registrations cannot succeed without both configured gateway secrets.
- The gateway handles only `1616255.xyz/ghactions-rss/*`; the existing `/rsshub/*` service is preserved.

These decisions replace the old Phase 1 policy and its source-copy-only restrictions, as explicitly requested for the new architecture.
