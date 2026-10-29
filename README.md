# ghactions-rss

Public execution node for official RSSHub Docker and the unified Cloudflare Pages publication. The private `Grinch27/cloudflare-RSSHUB_reader` repository owns subscriptions, generated XML, reader code, gateways, execution reports and personal state. Only `main` is used; this public repository contains no production subscription list or generated artifacts.

The RSS gateway records an exact RSSHub path and query in the private repository. The single workflow refreshes enabled registrations at **00:00, 06:00, 12:00 and 18:00 Asia/Shanghai**. GitHub schedules run asynchronously and may be delayed. Registration does not dispatch a workflow or build Pages.

Each cycle checks out private main with `READONLY`, runs `diygod/rsshub:chromium-bundled`, validates RSS/Atom, retains valid previous XML on source failures, and saves with `WRITEONLY`. Changed public content or enabled membership produces one snapshot commit; no-change cycles retain all public bytes, snapshot ID and generation time, and save private state only. Pages Git integration builds `site/`; this workflow no longer uploads Pages directly.

A unique cycle ID and matching completion report guard every public save. Failed directory replacement restores the previous snapshot; aborted cycles can save private state only. Production builds reject missing snapshots and missing or invalid XML declared available. Feed refresh clocks are ignored when comparing content, while article edits, dates, IDs, ordering and membership changes remain publishable.

The workflow verifies the Pages snapshot and XML, the Reader mount, and existing RSS gateway GET/HEAD responses. Detailed reports stay private; feed XML, Docker logs, private routes and tokens are not uploaded as public artifacts.

Repository secrets:

| Name | Purpose |
| --- | --- |
| `READONLY` | Fine-grained token for `Grinch27/cloudflare-RSSHUB_reader`, Contents read |
| `WRITEONLY` | Fine-grained token for the same repository, Contents read and write |
| `RSSHUB_ENV` | Optional multiline Docker environment file for upstream credentials |

`WRITEONLY` names a purpose; GitHub Contents write includes read access. Credentials are configured manually. Existing Cloudflare deployment secrets are retained but are unused by this Git-based publication workflow. Worker runtime secrets are independent and must be authorized separately; this workflow does not modify Worker source, routes or secrets.

Workflow-file pushes to main start a complete refresh. Manual Run workflow does the same by default. After fixing and retrying a failed Pages deployment once, select **verify_existing=true** to verify the current snapshot without starting Docker, fetching upstreams or generating a new snapshot ID. Verification results are saved as private state. Do not dispatch a second run while the push-triggered run is still active.

The canonical workflow source is `integration/ghactions-rss/rss_allinone.yml` in the private repository. Legacy generator files in this public repository remain for history and are not used by the new production workflow. Old repositories, Pages and Workers remain available; their retirement requires separate approval.
