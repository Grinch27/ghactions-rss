# ghactions-rss

Generic execution node for official RSSHub Docker and public Cloudflare Pages snapshots. Real subscriptions, generated XML, gateway source and detailed execution state live in a separate private repository, on `main` only. This public repository contains no production subscription list or generated artifacts.

The gateway registers an exact RSSHub path and query string in the private repository. GitHub Actions collects all enabled registrations at **00:00, 06:00, 12:00 and 18:00 Asia/Shanghai**. GitHub schedules start asynchronously and can be delayed. Registration does not dispatch a workflow or deploy Pages.

Each cycle checks out private `main` using `READONLY`, runs the official `diygod/rsshub:chromium-bundled` image, validates RSS/Atom, fixes the public self URL, preserves valid previous XML on individual failures, and commits `site/` plus `state/` with `WRITEONLY`. Only after saving does it upload `site/` to the new Pages project. Detailed reports stay private. No feed XML, Docker logs or reports are uploaded as public Actions artifacts.

Repository secrets:

| Name | Purpose |
| --- | --- |
| `READONLY` | Fine-grained token for the data repository, Contents read |
| `WRITEONLY` | Fine-grained token for the data repository, Contents read and write |
| `CLOUDFLARE_ACCOUNT_ID` | Existing deployment account |
| `CLOUDFLARE_API_TOKEN` | Pages deployment; Workers Scripts edit and zone Workers Routes edit when deploying the gateway |
| `RSSHUB_ENV` | Optional multiline Docker environment file containing provider cookies/tokens |

`WRITEONLY` is a credential name; GitHub Contents write access includes read access. Keep all token values out of source and logs. The optional gateway deployment injects the two GitHub secrets into the Worker using Wrangler's secrets input.

Normal code pushes run dependency-free generator tests. A maintenance commit containing `[refresh]` also starts a complete refresh. `[gateway]` together with `[refresh]` additionally deploys private gateway source. Manual Run workflow refreshes all subscriptions; enable `deploy_gateway` only for gateway updates. No arbitrary route input is exposed in the public workflow.

The target private repository and new Pages project are configured in the workflow. There is no data/publication branch. The previous Pages project and `/rsshub/*` service are preserved.

Local validation: `python3 -m unittest discover -s tests -v`.
