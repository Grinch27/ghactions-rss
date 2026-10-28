# Operating the private snapshot pipeline

1. Keep all repository heads on `main`.
2. Put exact subscriptions only in the private data repository. Each file is `subscriptions/<id-prefix>/<id>.json`; ID is SHA-256 of UTF-8 `v1\n` plus the exact relative route including query string.
3. Run the public generator tests and the private gateway tests before source deployment.
4. Schedule four complete snapshots daily in Asia/Shanghai. Use manual execution or a `[refresh]` maintenance commit for migration/repair.
5. Read private `main` with `READONLY`; save `site/` and `state/` with `WRITEONLY`. Never inspect or print token values.
6. Publish only `site/`, after a successful private push. Use normal main push/rebase to retain subscriptions registered during a run.
7. Verify the expected snapshot marker, valid XML and public self URLs. Save deployment evidence privately.
8. Deploy private gateway source separately through the connected Cloudflare API or Wrangler. Preserve manually configured Worker secrets. Registration does not rebuild the gateway. The Pages deployment token needs no Worker administration permissions.
9. A green pipeline may contain `PRESERVED`/`UNAVAILABLE` feeds. Read `state/feeds/` and last-success timestamps in the private repository to assess source freshness.

Do not publish registry files, provider environment variables, detailed reports or Docker logs in public artifacts. Do not rewrite repository history, create output branches, or alter unrelated production routes.
