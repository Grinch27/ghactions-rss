# Architecture Decisions

## Locked user decisions

- D1: `ghactions-rss` is intended to expand beyond JavBus RSSHub routes; JavBus is the pilot.
- D4: GitHub Actions directly runs official RSSHub.
- D12: use RSSHub `latest` and record its digest. This implementation resolves `latest` once and uses that exact digest throughout the run.
- D19: route templates are delegated to RSSHub; exact feed instances are explicitly materialized by `feeds.json`.
- D23: do not implement a separate language model; pass the configured native RSSHub path unchanged.
- D24/D25: Phase 1 does not support alternate JavBus domains because all query strings are forbidden.
- D33: refresh once per hour.
- D42: no feed-level retry.
- D45: feeds are independent; a failed feed must not prevent unrelated feeds from updating when the previous state can be safely preserved.
- D46: final public feed paths are extensionless and mirror RSSHub paths.
- D73: deploy with `cloudflare/wrangler-action@main`.
- D74: do not specify `wranglerVersion`.
- D75: record the observed `wrangler-action@main` HEAD and actual Wrangler `--version` output on every run.
- Base workflow structure: derived from the supplied `uboot_allinone.yml` architecture: `runner-image -> resolver -> action -> publication`.

## Workflow mapping from uboot_allinone

```text
U-Boot                         ghactions-rss
runner-image               -> runner-image
toolchain                  -> rsshub-image
action / compile cycle     -> action / Generate RSS Cycle
release.md                 -> generation-report.md + .json
GitHub Release             -> short-lived Artifact
direct release publication -> isolated deploy job
```

The privileged U-Boot build container, `/dev`/`/sys` mounts, unrestricted capabilities, and `permissions: write-all` are intentionally not inherited.

## Conservative operational defaults used by this package

These were required to make D45 deterministic without adding R2/KV/state branches:

- Previous feed source: current production Cloudflare Pages URL.
- If the new refresh succeeds, it wins even if the previous Pages copy cannot be read.
- If the new refresh fails and a valid previous copy exists, preserve it.
- If the new refresh fails and the previous path is a definite 404, mark the feed `UNAVAILABLE` and continue with unrelated feeds.
- If the new refresh fails and the previous state cannot be safely determined (network error, 5xx, invalid XML, etc.), mark it `BLOCKED` and stop before deployment to avoid accidentally deleting an existing feed.
- Removing a feed from `feeds.json` is an explicit deletion in the next successful deployment.

## No automatic additions

Phase 1 does not add:

- Cache API
- KV
- D1
- R2
- Durable Objects
- Pages Functions
- GitHub Release
- output/data branch
- RSS output commits
- automatic retries
- automatic route discovery
- alternate JavBus domains
- query parameters
