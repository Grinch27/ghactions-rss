import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("rss_site", Path(__file__).parents[1] / "scripts/rss_site.py")
rss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rss)
XML = b'<rss version="2.0"><channel><title>Example</title><item><guid>stable</guid><link>https://example.org/item</link></item></channel></rss>'
SETTINGS = {"schema_version": 2, "public_base_url": "https://example.org/ghactions-rss", "pages_base_url": "https://example.pages.dev",
            "rsshub_image": "diygod/rsshub:chromium-bundled", "max_workers": 4, "provider_interval_seconds": 0,
            "request_timeout_seconds": 10, "max_response_bytes": 5000000}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        rss.write_json(self.directory / "settings.json", SETTINGS)

    def tearDown(self):
        self.temp.cleanup()

    def feed(self, route="/example/topic?limit=3&filter=a%26b"):
        output = rss.route_output(route)
        identifier = rss.feed_id(route)
        record = {"schema_version": 2, "route": route, "output": output, "enabled": True, "allow_empty": True}
        rss.write_json(self.directory / "subscriptions" / identifier[:2] / (identifier + ".json"), record)
        return {**record, "id": identifier}

    def test_query_identity_and_public_self_preserve_entry_identity(self):
        feed = self.feed()
        self.assertNotEqual(feed["id"], rss.feed_id("/example/topic?filter=a%26b&limit=3"))
        result = rss.normalize_feed(XML, feed, SETTINGS["public_base_url"])
        channel = ET.fromstring(result).find("channel")
        self.assertEqual(channel.find("item/guid").text, "stable")
        self.assertEqual(channel.find("item/link").text, "https://example.org/item")
        self.assertEqual(channel.find(f"{{{rss.ATOM}}}link").get("href"), SETTINGS["public_base_url"] + feed["route"])

    def test_atom_self_is_replaced_without_modifying_entry_link(self):
        feed = self.feed("/example/topic?format=atom")
        body = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Example</title><link rel="self" href="http://127.0.0.1:1200/internal"/><entry><id>stable</id><link href="https://example.org/item"/></entry></feed>'
        result = rss.normalize_feed(body, feed, SETTINGS["public_base_url"])
        root = ET.fromstring(result)
        self.assertEqual(root.find(f"{{{rss.ATOM}}}link").get("href"), SETTINGS["public_base_url"] + feed["route"])
        self.assertEqual(root.find(f"{{{rss.ATOM}}}entry/{{{rss.ATOM}}}link").get("href"), "https://example.org/item")

    def test_empty_feed_allowed_but_doctype_and_wrong_format_rejected(self):
        rss.parse_feed(b"<rss><channel/></rss>", "rss")
        for body in (b"<!DOCTYPE rss><rss><channel/></rss>", b"<html/>"):
            with self.assertRaises(rss.CycleError):
                rss.parse_feed(body, "rss")
        with self.assertRaises(rss.CycleError):
            rss.parse_feed(b"<rss><channel/></rss>", "rss", False)

    def test_failed_refresh_preserves_exact_baseline_and_last_success(self):
        feed = self.feed()
        target = self.directory / "site/feeds/rss" / (feed["id"] + ".xml")
        target.parent.mkdir(parents=True)
        target.write_bytes(XML)
        rss.write_json(self.directory / "state/feeds" / (feed["id"] + ".json"), {"last_success_at": "2026-01-01T00:00:00Z"})
        def failing(*args):
            raise rss.CycleError("RSSHUB_HTTP_503")
        self.assertTrue(rss.generate(self.directory, "http://127.0.0.1:1200", failing))
        self.assertEqual(target.read_bytes(), XML)
        state = rss.read_json(self.directory / "state/feeds" / (feed["id"] + ".json"))
        self.assertEqual(state["status"], "PRESERVED")
        self.assertEqual(state["last_success_at"], "2026-01-01T00:00:00Z")

    def test_invalid_baseline_blocks_and_keeps_current_site_untouched(self):
        feed = self.feed()
        target = self.directory / "site/feeds/rss" / (feed["id"] + ".xml")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"corrupt baseline")
        def failing(*args):
            raise rss.CycleError("RSSHUB_HTTP_503")
        self.assertFalse(rss.generate(self.directory, "http://127.0.0.1:1200", failing))
        self.assertEqual(target.read_bytes(), b"corrupt baseline")
        self.assertEqual(rss.read_json(self.directory / "state/latest-cycle.json")["counts"]["BLOCKED"], 1)

    def test_unavailable_new_feed_does_not_prevent_another_feed_publishing(self):
        one = self.feed("/example/one")
        two = self.feed("/other/two")
        def fetcher(base, feed, settings):
            if feed["id"] == one["id"]:
                raise rss.CycleError("RSSHUB_HTTP_404")
            return rss.normalize_feed(XML, feed, settings["public_base_url"])
        self.assertTrue(rss.generate(self.directory, "http://127.0.0.1:1200", fetcher))
        self.assertFalse((self.directory / "site/feeds/rss" / (one["id"] + ".xml")).exists())
        self.assertTrue((self.directory / "site/feeds/rss" / (two["id"] + ".xml")).exists())
        public_files = [str(p.relative_to(self.directory / "site")) for p in (self.directory / "site").rglob("*") if p.is_file()]
        self.assertFalse(any("subscriptions" in p or "state" in p for p in public_files))
        self.assertEqual(len((self.directory / "site/_headers").read_text().splitlines()), 8)

    def test_same_provider_requests_are_serialized(self):
        for route in ("/example/one", "/example/two", "/other/one", "/other/two"):
            self.feed(route)
        lock = threading.Lock()
        active, maximum = {}, {}
        def fetcher(base, feed, settings):
            provider = feed["route"].split("/")[1]
            with lock:
                active[provider] = active.get(provider, 0) + 1
                maximum[provider] = max(maximum.get(provider, 0), active[provider])
            time.sleep(0.03)
            with lock:
                active[provider] -= 1
            return rss.normalize_feed(XML, feed, settings["public_base_url"])
        self.assertTrue(rss.generate(self.directory, "http://127.0.0.1:1200", fetcher))
        self.assertEqual(maximum, {"example": 1, "other": 1})

    def test_registration_path_mismatch_is_rejected(self):
        feed = self.feed()
        file = next((self.directory / "subscriptions").glob("*/*.json"))
        file.rename(file.with_name("0" * 64 + ".json"))
        with self.assertRaises(rss.CycleError):
            rss.load_configuration(self.directory)

    def test_git_push_rebase_preserves_registration_created_during_cycle(self):
        root = self.directory
        remote = root / "remote.git"
        producer, registration = root / "producer", root / "registration"
        def git(path, *args):
            result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "fixture git operation failed")
            return result.stdout.strip()
        git(root, "init", "--bare", "--initial-branch=main", str(remote))
        git(root, "clone", str(remote), str(producer))
        git(producer, "config", "user.name", "test")
        git(producer, "config", "user.email", "test@example.org")
        (producer / "site").mkdir()
        (producer / "site/index.html").write_text("initial")
        (producer / "state").mkdir()
        (producer / "state/.gitkeep").write_text("")
        git(producer, "add", ".")
        git(producer, "commit", "-m", "initial")
        git(producer, "push", "origin", "main")
        git(root, "clone", str(remote), str(registration))
        git(registration, "config", "user.name", "test")
        git(registration, "config", "user.email", "test@example.org")
        (registration / "subscriptions").mkdir()
        (registration / "subscriptions/new.json").write_text('{"new":true}')
        git(registration, "add", ".")
        git(registration, "commit", "-m", "register")
        git(registration, "push", "origin", "main")
        (producer / "site/index.html").write_text("snapshot")
        with patch.dict(os.environ, {"WRITEONLY": "local-test-token"}):
            rss.persist(producer)
        self.assertEqual((producer / "subscriptions/new.json").read_text(), '{"new":true}')
        self.assertEqual((producer / "site/index.html").read_text(), "snapshot")
        self.assertEqual(git(remote, "branch", "--format=%(refname:short)"), "main")


if __name__ == "__main__":
    unittest.main()
