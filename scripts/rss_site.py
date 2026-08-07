#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

USER_AGENT = "ghactions-rss/1.0"
MAX_ROUTE_LENGTH = 2048
URL_PATH_SAFE = "/%:@!$&'()*+,;=._~-"
MANIFEST_ROUTE = "/_feed-state.json"


class ConfigError(ValueError):
    pass


class FeedValidationError(ValueError):
    pass


@dataclass(frozen=True)
class FeedSpec:
    route: str
    allow_empty: bool


@dataclass
class FetchResult:
    ok: bool
    status: int | None
    body: bytes | None
    error: str | None


@dataclass
class FeedResult:
    route: str
    state: str
    selected: bool
    previous_status: int | None
    previous_valid: bool
    current_status: int | None
    current_valid: bool
    final_sha256: str | None
    error: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_route(route: str) -> str:
    if not isinstance(route, str):
        raise ConfigError("feed route must be a string")
    if route != "/":
        route = route.rstrip("/")
    if not route.startswith("/") or route.startswith("//"):
        raise ConfigError(f"route must be an absolute path only: {route!r}")
    if "?" in route or "#" in route or "\\" in route:
        raise ConfigError(f"query, fragment, and backslash are forbidden: {route!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in route):
        raise ConfigError(f"control character in route: {route!r}")
    if len(route) > MAX_ROUTE_LENGTH:
        raise ConfigError(f"route exceeds {MAX_ROUTE_LENGTH} characters: {route!r}")
    if any(ch.isspace() for ch in route):
        raise ConfigError(f"whitespace in route must be percent-encoded: {route!r}")

    decoded_segments = unquote(route).split("/")
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise ConfigError(f"path traversal segment is forbidden: {route!r}")
    return route



def route_url(base: str, route: str) -> str:
    return base.rstrip("/") + quote(route, safe=URL_PATH_SAFE)

def provider_matches(route: str, provider: str) -> bool:
    return route == provider or route.startswith(provider + "/")


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != 1:
        raise ConfigError("config version must be 1")

    providers_raw = raw.get("providers")
    feeds_raw = raw.get("feeds")
    if not isinstance(providers_raw, list) or not providers_raw:
        raise ConfigError("providers must be a non-empty array")
    if not isinstance(feeds_raw, list) or not feeds_raw:
        raise ConfigError("feeds must be a non-empty array")

    providers: list[str] = []
    for provider in providers_raw:
        provider = normalize_route(provider)
        if provider == "/":
            raise ConfigError("root provider is forbidden")
        providers.append(provider)
    if len(providers) != len(set(providers)):
        raise ConfigError("duplicate providers are forbidden")

    feeds: list[FeedSpec] = []
    seen: set[str] = set()
    for item in feeds_raw:
        if not isinstance(item, dict):
            raise ConfigError("each feed must be an object")
        route = normalize_route(item.get("route"))
        if route in seen:
            raise ConfigError(f"duplicate feed route: {route}")
        if not any(provider_matches(route, provider) for provider in providers):
            raise ConfigError(f"feed route is outside provider allowlist: {route}")
        allow_empty = item.get("allow_empty", False)
        if not isinstance(allow_empty, bool):
            raise ConfigError(f"allow_empty must be boolean for {route}")
        seen.add(route)
        feeds.append(FeedSpec(route=route, allow_empty=allow_empty))

    feed_interval_seconds = raw.get("feed_interval_seconds", 2)
    request_timeout_seconds = raw.get("request_timeout_seconds", 60)
    max_response_bytes = raw.get("max_response_bytes", 5_000_000)

    if not isinstance(feed_interval_seconds, int) or not 0 <= feed_interval_seconds <= 60:
        raise ConfigError("feed_interval_seconds must be an integer in 0..60")
    if not isinstance(request_timeout_seconds, int) or not 1 <= request_timeout_seconds <= 300:
        raise ConfigError("request_timeout_seconds must be an integer in 1..300")
    if not isinstance(max_response_bytes, int) or not 1024 <= max_response_bytes <= 25_000_000:
        raise ConfigError("max_response_bytes must be an integer in 1024..25000000")

    return {
        "version": 1,
        "providers": providers,
        "feeds": feeds,
        "feed_interval_seconds": feed_interval_seconds,
        "request_timeout_seconds": request_timeout_seconds,
        "max_response_bytes": max_response_bytes,
    }


def validate_scope(config: dict[str, Any], scope: str) -> str:
    scope = scope.strip()
    if scope.lower() == "all":
        return "all"
    scope = normalize_route(scope)
    routes = {feed.route for feed in config["feeds"]}
    if scope not in routes:
        raise ConfigError(f"scope route is not configured in feeds.json: {scope}")
    return scope


def route_output_path(output: Path, route: str) -> Path:
    relative = route.lstrip("/") + ".html"
    path = output / relative
    resolved_output = output.resolve()
    resolved_parent = path.parent.resolve()
    if resolved_output != resolved_parent and resolved_output not in resolved_parent.parents:
        raise ConfigError(f"output path escaped dist: {route}")
    return path


def request_once(url: str, timeout: int, max_bytes: int) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return FetchResult(False, status, None, "RESPONSE_TOO_LARGE")
            return FetchResult(200 <= status < 300, status, body, None if 200 <= status < 300 else f"HTTP_{status}")
    except urllib.error.HTTPError as exc:
        return FetchResult(False, int(exc.code), None, f"HTTP_{exc.code}")
    except urllib.error.URLError as exc:
        return FetchResult(False, None, None, f"NETWORK_{type(exc.reason).__name__}")
    except TimeoutError:
        return FetchResult(False, None, None, "NETWORK_TIMEOUT")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validate_feed_xml(body: bytes, allow_empty: bool) -> None:
    if not body or not body.strip():
        raise FeedValidationError("EMPTY_BODY")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedValidationError("XML_PARSE_ERROR") from exc

    root_name = local_name(root.tag)
    if root_name == "rss":
        channel = next((child for child in root if local_name(child.tag) == "channel"), None)
        if channel is None:
            raise FeedValidationError("RSS_CHANNEL_MISSING")
        names = {local_name(child.tag) for child in channel}
        if "title" not in names or "link" not in names:
            raise FeedValidationError("RSS_CHANNEL_REQUIRED_FIELD_MISSING")
        item_count = sum(1 for child in channel if local_name(child.tag) == "item")
        if not allow_empty and item_count == 0:
            raise FeedValidationError("RSS_ITEMS_EMPTY")
        return

    if root_name == "feed":
        names = {local_name(child.tag) for child in root}
        if "title" not in names:
            raise FeedValidationError("ATOM_TITLE_MISSING")
        entry_count = sum(1 for child in root if local_name(child.tag) == "entry")
        if not allow_empty and entry_count == 0:
            raise FeedValidationError("ATOM_ENTRIES_EMPTY")
        return

    raise FeedValidationError(f"UNSUPPORTED_XML_ROOT_{root_name}")


def validate_fetch(fetch: FetchResult, allow_empty: bool) -> tuple[bool, str | None]:
    if not fetch.ok or fetch.body is None:
        return False, fetch.error
    try:
        validate_feed_xml(fetch.body, allow_empty)
        return True, None
    except FeedValidationError as exc:
        return False, str(exc)


def validate_manifest(fetch: FetchResult) -> tuple[bool, set[str], str | None]:
    if not fetch.ok or fetch.body is None:
        return False, set(), fetch.error
    try:
        raw = json.loads(fetch.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, set(), "MANIFEST_JSON_INVALID"
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return False, set(), "MANIFEST_SCHEMA_INVALID"
    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, list):
        return False, set(), "MANIFEST_ROUTES_INVALID"
    try:
        routes = [normalize_route(route) for route in routes_raw]
    except ConfigError:
        return False, set(), "MANIFEST_ROUTE_INVALID"
    if len(routes) != len(set(routes)):
        return False, set(), "MANIFEST_ROUTES_DUPLICATE"
    return True, set(routes), None


def write_static_support(output: Path, providers: list[str], routes: list[str]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    headers: list[str] = []
    common = [
        "  Content-Type: application/rss+xml; charset=utf-8",
        "  X-Content-Type-Options: nosniff",
        "  X-Robots-Tag: noindex",
    ]
    for provider in providers:
        headers.append(provider)
        headers.extend(common)
        headers.append("")
        headers.append(provider + "/*")
        headers.extend(common)
        headers.append("")
    (output / "_headers").write_text("\n".join(headers).rstrip() + "\n", encoding="utf-8")
    (output / "404.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>404</title><h1>404</h1>\n",
        encoding="utf-8",
    )
    (output / MANIFEST_ROUTE.lstrip("/")).write_text(
        json.dumps({"schema_version": 1, "routes": routes}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def generate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    scope = validate_scope(config, args.scope)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    timeout = config["request_timeout_seconds"]
    max_bytes = config["max_response_bytes"]
    interval = config["feed_interval_seconds"]
    pages_base = args.pages_base.rstrip("/")
    rsshub_base = args.rsshub_base.rstrip("/")

    results: list[FeedResult] = []
    blocked = False
    selected_seen = 0
    configured_routes = [feed.route for feed in config["feeds"]]

    previous_manifest = request_once(
        route_url(pages_base, MANIFEST_ROUTE), timeout, max_bytes
    )
    manifest_valid, previous_routes, manifest_error = validate_manifest(previous_manifest)
    if previous_manifest.status == 404:
        previous_routes = set()
        manifest_error = None
    elif not manifest_valid:
        blocked = True

    for feed in config["feeds"]:
        selected = scope == "all" or feed.route == scope
        previous = request_once(route_url(pages_base, feed.route), timeout, max_bytes)
        previous_valid, previous_error = validate_fetch(previous, feed.allow_empty)

        current = FetchResult(False, None, None, "NOT_SELECTED")
        current_valid = False
        current_error: str | None = "NOT_SELECTED"

        if selected:
            if selected_seen and interval:
                time.sleep(interval)
            selected_seen += 1
            current = request_once(route_url(rsshub_base, feed.route), timeout, max_bytes)
            current_valid, current_error = validate_fetch(current, feed.allow_empty)

        final_body: bytes | None = None
        state: str
        error: str | None = None

        if selected and current_valid and current.body is not None:
            final_body = current.body
            state = "UPDATED"
        elif previous_valid and previous.body is not None:
            final_body = previous.body
            state = "PRESERVED"
            error = current_error if selected else None
        elif previous.status == 404:
            state = "UNAVAILABLE"
            error = current_error if selected else "NOT_SELECTED_AND_NO_PREVIOUS"
        else:
            state = "BLOCKED"
            blocked = True
            error = current_error or previous_error or "PREVIOUS_UNAVAILABLE_AND_CURRENT_INVALID"

        final_sha256: str | None = None
        if final_body is not None:
            path = route_output_path(output, feed.route)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(final_body)
            final_sha256 = sha256_bytes(final_body)

        results.append(
            FeedResult(
                route=feed.route,
                state=state,
                selected=selected,
                previous_status=previous.status,
                previous_valid=previous_valid,
                current_status=current.status,
                current_valid=current_valid,
                final_sha256=final_sha256,
                error=error,
            )
        )

        print(
            f"{feed.route}: state={state} selected={selected} "
            f"previous={previous.status} current={current.status}"
        )

    for route in sorted(previous_routes - set(configured_routes)):
        results.append(
            FeedResult(
                route=route,
                state="REMOVED",
                selected=False,
                previous_status=None,
                previous_valid=True,
                current_status=None,
                current_valid=False,
                final_sha256=None,
                error="REMOVED_FROM_CONFIG",
            )
        )
        print(f"{route}: state=REMOVED selected=False previous=manifest current=None")

    write_static_support(output, config["providers"], configured_routes)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.state] = counts.get(result.state, 0) + 1

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "repository": args.repository,
        "commit": args.commit,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "scope": scope,
        "rsshub_image_ref": args.rsshub_image_ref,
        "rsshub_image_id": args.rsshub_image_id,
        "pages_base_url": pages_base,
        "rsshub_base_url": rsshub_base,
        "counts": counts,
        "blocked": blocked,
        "baseline_manifest": {
            "status": previous_manifest.status,
            "valid": manifest_valid,
            "error": manifest_error,
        },
        "feeds": [asdict(result) for result in results],
    }
    Path(args.report_json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# RSS Generation Report",
        "",
        f"- Timestamp (UTC): `{report['generated_at']}`",
        f"- Repository: `{args.repository}`",
        f"- Commit: `{args.commit}`",
        f"- Run: `{args.run_id}` attempt `{args.run_attempt}`",
        f"- Scope: `{scope}`",
        f"- RSSHub image: `{args.rsshub_image_ref}`",
        f"- Blocked: `{str(blocked).lower()}`",
        "",
        "## Summary",
        "",
    ]
    for state in ("UPDATED", "PRESERVED", "UNAVAILABLE", "REMOVED", "BLOCKED"):
        lines.append(f"- {state}: `{counts.get(state, 0)}`")
    lines.extend(["", "## Feeds", ""])
    for result in results:
        suffix = f" — `{result.error}`" if result.error else ""
        lines.append(f"- `{result.state}` `{result.route}`{suffix}")
    Path(args.report_md).write_text("\n".join(lines) + "\n", encoding="utf-8")

    if blocked:
        print("One or more feeds could not be safely regenerated or preserved.", file=sys.stderr)
        return 2
    return 0


def validate_site(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    output = Path(args.output)
    report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    by_route = {item["route"]: item for item in report.get("feeds", [])}

    if report.get("blocked"):
        raise FeedValidationError("artifact report is blocked")

    for feed in config["feeds"]:
        item = by_route.get(feed.route)
        if item is None:
            raise FeedValidationError(f"missing report entry: {feed.route}")
        path = route_output_path(output, feed.route)
        state = item["state"]
        if state in {"UPDATED", "PRESERVED"}:
            if not path.is_file():
                raise FeedValidationError(f"missing output for {state}: {feed.route}")
            body = path.read_bytes()
            validate_feed_xml(body, feed.allow_empty)
            actual_hash = sha256_bytes(body)
            if actual_hash != item.get("final_sha256"):
                raise FeedValidationError(f"hash mismatch: {feed.route}")
        elif state == "UNAVAILABLE":
            if path.exists():
                raise FeedValidationError(f"UNAVAILABLE feed unexpectedly has output: {feed.route}")
        else:
            raise FeedValidationError(f"unsupported artifact state {state}: {feed.route}")

    configured_routes = {feed.route for feed in config["feeds"]}
    for route, item in by_route.items():
        if route in configured_routes:
            continue
        if item.get("state") != "REMOVED":
            raise FeedValidationError(f"unexpected unconfigured report route: {route}")
        if route_output_path(output, route).exists():
            raise FeedValidationError(f"REMOVED feed unexpectedly has output: {route}")

    if not all((output / name).is_file() for name in ("_headers", "404.html", MANIFEST_ROUTE.lstrip("/"))):
        raise FeedValidationError("static support files are missing")
    print("Static RSS site validation passed.")
    return 0


def smoke(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    by_route = {item["route"]: item for item in report.get("feeds", [])}
    timeout = config["request_timeout_seconds"]
    max_bytes = config["max_response_bytes"]
    base_url = args.base_url.rstrip("/")

    failures = 0
    for feed in config["feeds"]:
        item = by_route.get(feed.route)
        if item is None:
            print(f"Missing report entry for {feed.route}", file=sys.stderr)
            failures += 1
            continue
        result = request_once(route_url(base_url, feed.route), timeout, max_bytes)
        state = item["state"]

        if state in {"UPDATED", "PRESERVED"}:
            valid, error = validate_fetch(result, feed.allow_empty)
            if not valid or result.body is None:
                print(f"Smoke failed {feed.route}: {error}", file=sys.stderr)
                failures += 1
                continue
            actual_hash = sha256_bytes(result.body)
            if actual_hash != item.get("final_sha256"):
                print(f"Smoke hash mismatch {feed.route}", file=sys.stderr)
                failures += 1
                continue
            print(f"Smoke passed {feed.route}: {state}")
        elif state == "UNAVAILABLE":
            if result.status != 404:
                print(f"Expected 404 for unavailable feed {feed.route}, got {result.status}", file=sys.stderr)
                failures += 1
            else:
                print(f"Smoke passed {feed.route}: UNAVAILABLE=404")
        else:
            print(f"Unexpected state in smoke report: {state}", file=sys.stderr)
            failures += 1

    configured_routes = {feed.route for feed in config["feeds"]}
    for route, item in by_route.items():
        if route in configured_routes or item.get("state") != "REMOVED":
            continue
        result = request_once(route_url(base_url, route), timeout, max_bytes)
        if result.status != 404:
            print(f"Expected 404 for removed feed {route}, got {result.status}", file=sys.stderr)
            failures += 1
        else:
            print(f"Smoke passed {route}: REMOVED=404")

    if failures:
        return 1
    return 0


def validate_config_cmd(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    scope = validate_scope(config, args.scope)
    print(f"Config valid: providers={len(config['providers'])} feeds={len(config['feeds'])} scope={scope}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and validate a static RSSHub snapshot site")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate-config")
    p.add_argument("--config", required=True)
    p.add_argument("--scope", default="all")
    p.set_defaults(func=validate_config_cmd)

    p = sub.add_parser("generate")
    p.add_argument("--config", required=True)
    p.add_argument("--rsshub-base", required=True)
    p.add_argument("--pages-base", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--scope", default="all")
    p.add_argument("--report-json", required=True)
    p.add_argument("--report-md", required=True)
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    p.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    p.add_argument("--rsshub-image-ref", default="")
    p.add_argument("--rsshub-image-id", default="")
    p.set_defaults(func=generate)

    p = sub.add_parser("validate-site")
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--report-json", required=True)
    p.set_defaults(func=validate_site)

    p = sub.add_parser("smoke")
    p.add_argument("--config", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--report-json", required=True)
    p.set_defaults(func=smoke)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (ConfigError, FeedValidationError, json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
