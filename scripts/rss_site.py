#!/usr/bin/env python3
"""Private snapshot builder. Public stdout contains counts and fixed error codes only."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as ET

ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("atom", ATOM)
XML_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}


class CycleError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CycleError("INVALID_PRIVATE_JSON") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def feed_id(route):
    return hashlib.sha256(("v1\n" + route).encode()).hexdigest()


def route_output(route):
    if not isinstance(route, str) or len(route) > 4096 or not route.startswith("/") or route.startswith("//"):
        raise CycleError("INVALID_ROUTE")
    if re.search(r"[\\\x00-\x20\x7f#]", route):
        raise CycleError("INVALID_ROUTE")
    parsed = urlsplit(route)
    for segment in parsed.path.split("/"):
        try:
            decoded = unquote(segment, errors="strict")
        except UnicodeError as exc:
            raise CycleError("INVALID_ROUTE") from exc
        if decoded in (".", "..") or re.search(r"[\x00-\x1f\x7f]", decoded):
            raise CycleError("INVALID_ROUTE")
    formats = [v for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k == "format"]
    if len(formats) > 1 or (formats and formats[0] not in ("rss", "atom")):
        raise CycleError("UNSUPPORTED_FORMAT")
    return formats[0] if formats else "rss"


def load_configuration(directory):
    settings = read_json(directory / "settings.json")
    if settings.get("schema_version") != 2:
        raise CycleError("INVALID_SETTINGS")
    for field in ("public_base_url", "pages_base_url"):
        url = urlsplit(settings.get(field, ""))
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise CycleError("INVALID_PUBLIC_BASE")
    limits = {"max_workers": (1, 8), "provider_interval_seconds": (0, 60),
              "request_timeout_seconds": (1, 120), "max_response_bytes": (1024, 25000000)}
    for key, (minimum, maximum) in limits.items():
        value = settings.get(key)
        if not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise CycleError("INVALID_LIMITS")
    if not isinstance(settings["max_workers"], int):
        raise CycleError("INVALID_LIMITS")
    image = settings.get("rsshub_image", "")
    if not re.fullmatch(r"diygod/rsshub:[a-zA-Z0-9_.-]+", image):
        raise CycleError("INVALID_IMAGE")
    feeds = []
    for path in sorted((directory / "subscriptions").glob("*/*.json")):
        if path.is_symlink():
            raise CycleError("INVALID_SUBSCRIPTION_FILE")
        record = read_json(path)
        route = record.get("route")
        output = route_output(route)
        identifier = feed_id(route)
        if (record.get("schema_version") != 2 or record.get("output") != output
                or path.stem != identifier or path.parent.name != identifier[:2]
                or not isinstance(record.get("enabled"), bool)
                or not isinstance(record.get("allow_empty", True), bool)):
            raise CycleError("INVALID_SUBSCRIPTION")
        if record["enabled"]:
            feeds.append({**record, "id": identifier})
    return settings, feeds


def parse_feed(body, output, allow_empty=True):
    if re.search(br"<!\s*(DOCTYPE|ENTITY)", body, re.I):
        raise CycleError("XML_DECLARATION_REJECTED")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError) as exc:
        raise CycleError("INVALID_XML") from exc
    if output == "atom":
        if root.tag != f"{{{ATOM}}}feed":
            raise CycleError("UNEXPECTED_XML_FORMAT")
        count = len(root.findall(f"{{{ATOM}}}entry"))
    else:
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise CycleError("UNEXPECTED_XML_FORMAT")
        count = len(channel.findall("item"))
    if not allow_empty and count == 0:
        raise CycleError("EMPTY_FEED_REJECTED")
    return root


def normalize_feed(body, feed, public_base):
    root = parse_feed(body, feed["output"], feed.get("allow_empty", True))
    parent = root if feed["output"] == "atom" else root.find("channel")
    for link in list(parent.findall(f"{{{ATOM}}}link")):
        if link.get("rel") == "self":
            parent.remove(link)
    ET.SubElement(parent, f"{{{ATOM}}}link", {"rel": "self", "href": public_base.rstrip("/") + feed["route"],
                  "type": "application/atom+xml" if feed["output"] == "atom" else "application/rss+xml"})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def fetch_feed(base, feed, settings):
    if urlsplit(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise CycleError("NONLOCAL_RSSHUB_REJECTED")
    request = Request(base.rstrip("/") + feed["route"], headers={"User-Agent": "ghactions-rss/2", "Accept": "application/rss+xml, application/atom+xml"})
    try:
        with urlopen(request, timeout=settings["request_timeout_seconds"]) as response:
            if response.status != 200 or response.headers.get_content_type() not in XML_TYPES:
                raise CycleError("RSSHUB_RESPONSE_INVALID")
            body = response.read(int(settings["max_response_bytes"]) + 1)
    except HTTPError as exc:
        raise CycleError("RSSHUB_HTTP_" + str(exc.code)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CycleError("RSSHUB_UNREACHABLE") from exc
    if len(body) > settings["max_response_bytes"]:
        raise CycleError("RSSHUB_RESPONSE_TOO_LARGE")
    return normalize_feed(body, feed, settings["public_base_url"])


def generated_static(candidate, snapshot):
    (candidate / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>RSS snapshots</title><p>Public RSS snapshots. No subscription directory.</p>\n', encoding="utf-8")
    (candidate / "404.html").write_text('<!doctype html><meta charset="utf-8"><title>Not found</title>Not found\n', encoding="utf-8")
    (candidate / "_headers").write_text(
        "/*\n  X-Robots-Tag: noindex, nofollow\n  X-Content-Type-Options: nosniff\n  Cache-Control: public, max-age=0, must-revalidate\n"
        "/feeds/rss/*\n  Content-Type: application/rss+xml; charset=utf-8\n"
        "/feeds/atom/*\n  Content-Type: application/atom+xml; charset=utf-8\n", encoding="utf-8")
    write_json(candidate / "_snapshot.json", snapshot)


def generate(directory, base, fetcher=fetch_feed):
    settings, feeds = load_configuration(directory)
    snapshot = {"schema_version": 2, "snapshot_id": uuid.uuid4().hex, "generated_at": now()}
    candidate = directory / ".candidate-site"
    if candidate.exists():
        shutil.rmtree(candidate)
    candidate.mkdir()
    locks = {feed["route"].split("/")[1]: threading.Lock() for feed in feeds}
    last_start = {}

    def refresh(feed):
        identifier = feed["id"]
        relative = Path("feeds") / feed["output"] / (identifier + ".xml")
        old_path = directory / "site" / relative
        state_path = directory / "state" / "feeds" / (identifier + ".json")
        previous = read_json(state_path) if state_path.exists() else {}
        old_body = None
        old_invalid = False
        if old_path.exists():
            try:
                if old_path.is_symlink() or old_path.stat().st_size > settings["max_response_bytes"]:
                    raise CycleError("INVALID_BASELINE")
                old_body = old_path.read_bytes()
                parse_feed(old_body, feed["output"], feed.get("allow_empty", True))
            except CycleError:
                old_body, old_invalid = None, True
        provider = feed["route"].split("/")[1]
        status, error, body = "UPDATED", None, None
        try:
            # One request per provider at a time, with a minimum start interval.
            with locks[provider]:
                delay = last_start.get(provider, 0) + settings["provider_interval_seconds"] - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                last_start[provider] = time.monotonic()
                body = fetcher(base, feed, settings)
        except CycleError as exc:
            error = str(exc)
            if old_body is not None:
                status, body = "PRESERVED", old_body
            else:
                status = "BLOCKED" if old_invalid else "UNAVAILABLE"
        if body is not None:
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        state = {"schema_version": 2, "id": identifier, "route": feed["route"], "status": status,
                 "last_attempt_at": snapshot["generated_at"], "last_success_at": snapshot["generated_at"] if status == "UPDATED" else previous.get("last_success_at"), "error": error}
        write_json(state_path, state)
        return state

    with ThreadPoolExecutor(max_workers=settings["max_workers"]) as executor:
        states = list(executor.map(refresh, feeds))
    counts = {status: sum(state["status"] == status for state in states)
              for status in ("UPDATED", "PRESERVED", "UNAVAILABLE", "BLOCKED")}
    publishable = not counts["BLOCKED"] and (not feeds or counts["UPDATED"] + counts["PRESERVED"] > 0)
    report = {**snapshot, "publishable": publishable, "counts": counts, "feeds": states,
              "runner_run_id": os.environ.get("GITHUB_RUN_ID"), "image_ref": os.environ.get("RSSHUB_IMAGE_REF")}
    write_json(directory / "state" / "latest-cycle.json", report)
    # One private report per UTC day; detailed per-feed status is retained in state/feeds.
    write_json(directory / "state" / "cycles" / (snapshot["generated_at"][:10] + ".json"), report)
    if publishable:
        generated_static(candidate, snapshot)
        old_site = directory / "site"
        if old_site.exists():
            shutil.rmtree(old_site)
        candidate.rename(old_site)
    else:
        shutil.rmtree(candidate)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"snapshot_id={snapshot['snapshot_id']}\npublishable={str(publishable).lower()}\n")
    print(json.dumps({"event": "cycle", "publishable": publishable, "counts": counts}))
    return publishable


def git(directory, *arguments, env=None, check=True):
    result = subprocess.run(["git", "-C", str(directory), *arguments], env=env, capture_output=True, text=True)
    if check and result.returncode:
        raise CycleError("PRIVATE_GIT_OPERATION_FAILED")
    return result


def persist(directory):
    if git(directory, "branch", "--show-current").stdout.strip() != "main":
        raise CycleError("MAIN_BRANCH_REQUIRED")
    token = os.environ.get("WRITEONLY", "")
    if not token:
        raise CycleError("WRITEONLY_NOT_CONFIGURED")
    credentials = base64.b64encode(("x-access-token:" + token).encode()).decode()
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader", "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + credentials}
    git(directory, "config", "user.name", "github-actions[bot]")
    git(directory, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    if git(directory, "diff", "--cached", "--name-only").stdout.strip():
        raise CycleError("UNEXPECTED_STAGED_CHANGES")
    git(directory, "add", "--", "site", "state")
    if git(directory, "diff", "--cached", "--quiet", check=False).returncode == 0:
        print('{"event":"private_snapshot_unchanged"}')
        return
    git(directory, "commit", "-m", "data: update RSS snapshot and execution state")
    for _ in range(4):
        if git(directory, "push", "origin", "HEAD:refs/heads/main", env=environment, check=False).returncode == 0:
            print('{"event":"private_snapshot_saved"}')
            return
        git(directory, "fetch", "origin", "main", env=environment)
        if git(directory, "rebase", "origin/main", env=environment, check=False).returncode:
            git(directory, "rebase", "--abort", check=False)
            raise CycleError("PRIVATE_GIT_CONFLICT")
    raise CycleError("PRIVATE_GIT_PUSH_EXHAUSTED")


def verify(directory, base):
    expected = read_json(directory / "site" / "_snapshot.json")
    settings, feeds = load_configuration(directory)
    for attempt in range(6):
        try:
            with urlopen(base.rstrip("/") + "/_snapshot.json?cycle=" + expected["snapshot_id"], timeout=20) as response:
                deployed = json.loads(response.read(4096))
            if deployed.get("snapshot_id") != expected["snapshot_id"]:
                raise CycleError("DEPLOYMENT_NOT_CURRENT")
            for feed in feeds:
                relative = Path("feeds") / feed["output"] / (feed["id"] + ".xml")
                if not (directory / "site" / relative).exists():
                    continue
                with urlopen(base.rstrip("/") + "/" + relative.as_posix(), timeout=20) as response:
                    if response.headers.get_content_type() not in XML_TYPES:
                        raise CycleError("PUBLISHED_CONTENT_TYPE_INVALID")
                    body = response.read(int(settings["max_response_bytes"]) + 1)
                if len(body) > settings["max_response_bytes"]:
                    raise CycleError("PUBLISHED_XML_TOO_LARGE")
                root = parse_feed(body, feed["output"], feed.get("allow_empty", True))
                parent = root if feed["output"] == "atom" else root.find("channel")
                if not any(link.get("rel") == "self" and link.get("href") == settings["public_base_url"].rstrip("/") + feed["route"]
                           for link in parent.findall(f"{{{ATOM}}}link")):
                    raise CycleError("PUBLISHED_SELF_INVALID")
            write_json(directory / "state" / "deployment.json", {"verified_at": now(), "snapshot_id": expected["snapshot_id"], "status": "success",
                       "deployment_id": os.environ.get("CF_DEPLOYMENT_ID"), "deployment_url": os.environ.get("CF_DEPLOYMENT_URL")})
            print('{"event":"published_snapshot_verified"}')
            return
        except (OSError, ValueError, CycleError) as exc:
            if attempt == 5:
                raise CycleError("PUBLICATION_VERIFICATION_FAILED") from exc
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "image", "generate", "persist", "verify"))
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--rsshub-base", default="http://127.0.0.1:1200")
    parser.add_argument("--base-url")
    args = parser.parse_args()
    directory = args.data_directory.resolve()
    try:
        if args.command in ("validate", "image"):
            settings, feeds = load_configuration(directory)
            print(settings["rsshub_image"] if args.command == "image" else json.dumps({"event": "configuration_valid", "subscriptions": len(feeds)}))
        elif args.command == "generate":
            return 0 if generate(directory, args.rsshub_base) else 2
        elif args.command == "persist":
            persist(directory)
        elif args.command == "verify":
            verify(directory, args.base_url)
    except Exception as exc:
        # Tracebacks, URLs and response bodies must not appear in the public job log.
        code = str(exc) if isinstance(exc, CycleError) else "CYCLE_INTERNAL_ERROR"
        print(json.dumps({"event": "error", "code": code}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
