# ghactions-rss

`ghactions-rss` uses GitHub Actions as a scheduled RSSHub execution environment and Cloudflare Pages as a static RSS publishing layer.

JavBus is the Phase 1 pilot provider. The framework is intentionally provider-agnostic so later phases can add other RSSHub route prefixes without merging them into a Cloudflare Worker runtime.

## Architecture

```text
GitHub Actions
  -> resolve dynamic runner
  -> resolve diygod/rsshub:latest to an immutable digest
  -> start that exact RSSHub image locally
  -> materialize routes from config/feeds.json
  -> validate RSS XML
  -> preserve the current Pages copy when an individual refresh fails
  -> upload the validated site as a short-lived GitHub Artifact
  -> deploy the Artifact to Cloudflare Pages with cloudflare/wrangler-action@v4
  -> production smoke test
```

Cloudflare Pages does not run RSSHub, does not contact JavBus, and does not contain RSSHub credentials. RSS readers only fetch static XML snapshots.

## Phase 1 policy

- Repository: PRIVATE.
- Cloudflare Pages: Direct Upload, not Git Integration.
- Pages Functions: disabled / unused.
- RSSHub: `diygod/rsshub:latest`, resolved once per run and frozen to the resulting digest for that run.
- Refresh interval: once per hour at minute 23 UTC.
- Feed refresh: sequential.
- Feed retry: none.
- Route source: explicit `config/feeds.json` only.
- Phase 1 provider allowlist: `/javbus`.
- Query strings and fragments: forbidden.
- Alternate `domain` / `western_domain`: therefore not supported in Phase 1.
- Final URL: extensionless, matching the RSSHub path as closely as possible.
- RSS output: unmodified RSSHub response bytes after validation.
- Application cache/KV/D1/R2: none.
- GitHub Release: unused.
- Generated RSS is not committed back to Git.

## Feed independence / Last-Known-Good

Each configured feed is refreshed independently.

- `UPDATED`: local RSSHub returned valid RSS; publish the new bytes.
- `PRESERVED`: refresh failed (or the feed was not selected by a manual run), but the current production Pages copy is valid; preserve it.
- `UNAVAILABLE`: there is no valid previous copy and the current refresh did not produce a valid feed; leave that path absent/404 without blocking unrelated feeds.
- `BLOCKED`: current refresh failed and the workflow could not safely determine/preserve the previous production state; stop before deployment.

Removing a route from `feeds.json` is treated as an explicit removal: it is not included in the next static site deployment.

## Project layout

```text
.github/workflows/rss_allinone.yml
config/feeds.json
scripts/rss_site.py
docs/DECISIONS.md
docs/CODEX_EXECUTION_PLAN.md
README.md
```

## Configure feeds

`config/feeds.json` is the production declaration.

`providers` is an allowlist of RSSHub route prefixes. Adding a provider only permits routes under that prefix to be declared; it does not automatically publish every route below that prefix. Every published route must still be listed explicitly in `feeds`.

Example:

```json
{
  "version": 1,
  "providers": [
    "/javbus",
    "/bilibili"
  ],
  "feed_interval_seconds": 2,
  "request_timeout_seconds": 60,
  "max_response_bytes": 5000000,
  "feeds": [
    {
      "route": "/javbus/star/rwt",
      "allow_empty": false
    },
    {
      "route": "/bilibili/popular/all",
      "allow_empty": false
    }
  ]
}
```

To configure another provider:

1. Add its route prefix to `providers`.
2. Add every exact route to be published to `feeds`.
3. Set `allow_empty` independently for each feed.

Adding `"/bilibili"` does not publish all `/bilibili/*` routes. Only routes explicitly present in `feeds` are materialized.

Configured routes are currently path-only. Query strings and routes requiring RSSHub provider secrets are outside the current scope.

To add another JavBus subpage, add another exact RSSHub route. The generator deliberately does not parse JavBus concepts such as `star`, `genre`, `series`, `language`, or `search`; RSSHub remains responsible for route semantics.

Examples of the intended model:

```text
/javbus
/javbus/star/<id>
/javbus/genre/<id>
/javbus/series/<id>
/javbus/studio/<id>
/javbus/label/<id>
/javbus/director/<id>
/javbus/search/<encoded-keyword>
/javbus/uncensored/...
/javbus/western/...
```

Only routes explicitly present in `feeds.json` are materialized.

## Cloudflare Pages setup

Create a **Direct Upload** Pages project once, preferably named `ghactions-rss`, with production branch `main`.

Direct Upload is intentional: the workflow produces prebuilt static assets and uploads them with Wrangler. Do not connect the Pages project to Git Integration.

Required GitHub repository secrets:

```text
CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_API_TOKEN
```

The API token should have the minimum Cloudflare Pages edit permission required for the target account.

Optional GitHub repository variables:

```text
PAGES_PROJECT=ghactions-rss
PAGES_BASE_URL=https://ghactions-rss.pages.dev
```

The defaults above are already embedded in the workflow. Set the variables only when the actual Pages project URL/name differs.

## Workflow triggers

- `push` to `main` when workflow/config/scripts change: refresh all feeds and deploy.
- `schedule`: refresh all feeds once per hour.
- `repository_dispatch` type `rss_generate`.
- `workflow_dispatch`: refresh all feeds or one exact configured route.

Manual single-feed refresh still builds a complete Pages snapshot: non-selected configured feeds are restored from the currently published Pages copy.

## Static URL mapping

The generator stores RSS bytes as `.html` assets so Cloudflare Pages can serve extensionless paths. `_headers` overrides their MIME type to:

```text
Content-Type: application/rss+xml; charset=utf-8
X-Content-Type-Options: nosniff
X-Robots-Tag: noindex
```

Example:

```text
RSSHub route: /javbus/star/rwt
Static file: dist/javbus/star/rwt.html
Published URL: https://ghactions-rss.pages.dev/javbus/star/rwt
```

A top-level `404.html` is generated so non-materialized routes return a real 404 instead of acting as an SPA fallback.

## Evidence model

Each generation run uploads `rss-site-build` for 7 days containing:

```text
dist/
generation-report.json
generation-report.md
rsshub-image.txt
SHA256SUMS.txt
```

The deploy job verifies `SHA256SUMS.txt` before Pages upload.

A separate `deployment-evidence` Artifact records:

```text
ghactions-rss commit
workflow run ID / attempt
cloudflare/wrangler-action@v4 observed tag commit
Wrangler version observed in the same `wrangler-action` invocation that performs the Pages deployment
Pages project
deployment ID
deployment URL
deployment alias URL
Pages environment
timestamp
```

`cloudflare/wrangler-action@v4` is intentionally mutable. The workflow records the observed `v4` tag commit immediately before use as evidence; GitHub does not expose a first-class variable containing the exact internally resolved action commit, so this is a best-effort race-minimized observation rather than a cryptographic pin. The branch ref `@main` is not used because its `action.yml` currently targets a generated `dist/index.mjs` that is absent from the branch, while the release tag contains the executable bundle.

## Important limitations

1. This is static materialization. An unknown JavBus path cannot be generated on first HTTP request; add it to `feeds.json` first.
2. No application-layer cache is implemented. Cloudflare Pages may apply its normal static asset/CDN behavior.
3. `latest` and `@main` are intentionally mutable. The project favors latest-first operation with recorded evidence, not strict reproducible builds.
4. A feed can only be preserved after refresh failure when its currently published Pages copy can be fetched and validated.
5. Phase 1 forbids URL query strings, so RSSHub route options expressed as query parameters are intentionally out of scope.
6. Cloudflare Pages documents that assets from a prior deployment can remain in a data-center cache for up to one week, so explicit feed removal is a deployment-state decision but may not become a globally observable 404 immediately.

## Local checks (optional)

No third-party Python package is required:

```bash
python3 scripts/rss_site.py validate-config \
  --config config/feeds.json \
  --scope all
```

The production workflow remains the authoritative execution path.
