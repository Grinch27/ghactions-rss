# Codex Execution Plan — ghactions-rss Phase 1

## Objective

Create a new PRIVATE repository named `ghactions-rss`, publish the exact GPT-delivered project without source rewrites, create/verify the GitHub Actions workflow, and drive the JavBus pilot through GitHub Actions -> Cloudflare Pages Direct Upload.

## Source ownership

GPT owns source design and source revisions.

Codex may:

- verify package hashes;
- create the PRIVATE repository if it does not exist;
- copy the delivered files exactly;
- run non-mutating syntax/static checks;
- commit and push `main`;
- observe Actions runs;
- collect sanitized logs/statuses;
- report blockers.

Codex must not independently rewrite `rss_allinone.yml`, `rss_site.py`, `feeds.json`, or project policy to make a failing run pass.

Source-level failure status:

```text
STOP_GPT_SOURCE_REVISION_REQUIRED
```

## External prerequisites

Cloudflare Pages must be a Direct Upload project. Preferred project name:

```text
ghactions-rss
```

Required GitHub repository secrets must be entered by the user or already exist:

```text
CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_API_TOKEN
```

Codex must never print or retrieve their values.

Optional repository variables:

```text
PAGES_PROJECT
PAGES_BASE_URL
```

Only set these when the actual project differs from the defaults.

## Execution gates

1. Verify the delivered ZIP SHA-256 and internal `SHA256SUMS.txt`.
2. Create or verify PRIVATE `ghactions-rss` repository with `main` as the only working branch.
3. Copy the GPT package exactly; no package.json/lockfile should be added.
4. Run:

```bash
python3 scripts/rss_site.py validate-config --config config/feeds.json --scope all
```

5. YAML parse/static review only; do not modify source.
6. Commit, for example:

```text
setup: add RSSHub static Pages pipeline
```

7. Push `main`.
8. Observe the workflow jobs:

```text
runner-image
rsshub-image
action
deploy
```

9. Confirm `rsshub-image` resolves `diygod/rsshub:latest` to an immutable digest.
10. Confirm `action` starts RSSHub by the resolved digest and processes `/javbus/star/rwt`.
11. Confirm Artifact `rss-site-build` contains the expected files and validates with `SHA256SUMS.txt`.
12. Confirm `deploy` uses `cloudflare/wrangler-action@v4` and does not set `wranglerVersion`.
13. Confirm Pages returns the feed at the extensionless route:

```text
/javbus/star/rwt
```

14. Confirm Content-Type contains `application/rss+xml` and XML is valid.
15. Confirm unknown route returns 404.
16. Record the observed RSSHub digest, wrangler-action v4 commit, Wrangler version, Pages deployment ID/URL/environment, and final commit.

## Do not do

- no manual `wrangler pages deploy` outside the workflow;
- no GitHub Release;
- no generated RSS commit;
- no Pages Git Integration;
- no Pages Functions;
- no retry loops;
- no alternate JavBus domain/query parameters;
- no public repository;
- no secret inspection;
- no source patching by Codex.

## Final states

Success:

```text
JAVBUS_STATIC_RSS_PHASE1_SUCCESS
```

Infrastructure/prerequisite block:

```text
STOP_EXTERNAL_PREREQUISITE_REQUIRED
```

Source failure:

```text
STOP_GPT_SOURCE_REVISION_REQUIRED
```

Report to:

```text
tmp/<RUN_ID>/CODEX_REPORT_EXEC.md
```
