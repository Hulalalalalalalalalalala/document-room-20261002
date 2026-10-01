import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from document_room import DocumentRoom

ROOT = Path(__file__).resolve().parent


class DocumentRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "policy.md").write_text("Retention policy\n", encoding="utf-8")
        self.index = self.root / "index.json"
        self.room = DocumentRoom(self.root, self.index)

    def test_metadata_and_conjunctive_search(self):
        record = self.room.add("policy.md", [" Legal ", "POLICY", "legal"])
        self.assertEqual(record["bytes"], 17)
        self.assertEqual(record["tags"], ["legal", "policy"])
        self.assertEqual(self.room.search(["legal", "policy"], "POLICY"), [record])
        self.assertEqual(self.room.search(["finance"]), [])

    def test_refresh_replaces_metadata(self):
        old = self.room.add("policy.md")
        (self.root / "policy.md").write_text("New policy", encoding="utf-8")
        new = self.room.add("policy.md", ["updated"])
        self.assertNotEqual(old["sha256"], new["sha256"])
        self.assertEqual(len(self.room.search()), 1)

    def test_outside_and_missing_rejected(self):
        for path in ("../README.md", "absent.txt"):
            with self.assertRaises(ValueError):
                self.room.add(path)
        self.assertFalse(self.index.exists())

    def test_cli_roundtrip_and_error(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        added = subprocess.run(prefix + ["add", "policy.md", "--tag", "legal"], capture_output=True, text=True)
        self.assertEqual(added.returncode, 0, added.stderr)
        found = subprocess.run(prefix + ["search", "--tag", "legal"], capture_output=True, text=True)
        self.assertEqual(json.loads(found.stdout)[0]["path"], "policy.md")
        self.assertEqual(subprocess.run(prefix + ["add", "missing"], capture_output=True).returncode, 2)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExportManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "same.txt").write_text("alpha content\n", encoding="utf-8")
        (self.root / "other").mkdir()
        (self.root / "other" / "same.txt").write_text("different content\n", encoding="utf-8")
        (self.root / "notes.md").write_text("meeting notes\n", encoding="utf-8")
        (self.root / "stale.md").write_text("old text\n", encoding="utf-8")
        (self.root / "gone.md").write_text("to be removed\n", encoding="utf-8")
        self.index = self.root / "index.json"
        self.room = DocumentRoom(self.root, self.index)
        self.room.add("docs/same.txt", ["Legal"])
        self.room.add("other/same.txt", ["ops"])
        self.room.add("notes.md", ["legal", "ops"])
        self.room.add("stale.md", ["ops"])
        self.room.add("gone.md", ["ops"])
        (self.root / "stale.md").write_text("new text\n", encoding="utf-8")
        (self.root / "gone.md").unlink()

    def _by_path(self, manifest):
        return {doc["path"]: doc for doc in manifest["documents"]}

    def test_statuses_ordering_and_completion(self):
        manifest = self.room.export_manifest("  Q4 Release  ")
        self.assertEqual(manifest["release"], "Q4 Release")
        self.assertFalse(manifest["complete"])
        self.assertEqual(set(manifest), {"release", "complete", "documents"})
        self.assertEqual([doc["path"] for doc in manifest["documents"]],
                         ["docs/same.txt", "gone.md", "notes.md", "other/same.txt", "stale.md"])
        docs = self._by_path(manifest)
        for path in ("docs/same.txt", "other/same.txt", "notes.md"):
            self.assertEqual(docs[path]["status"], "ready")
        self.assertEqual(docs["stale.md"]["status"], "changed")
        self.assertEqual(docs["gone.md"]["status"], "missing")
        for doc in manifest["documents"]:
            self.assertEqual(set(doc), {"path", "name", "bytes", "sha256", "tags", "status"})

    def test_same_name_different_paths_judged_independently(self):
        (self.root / "other" / "same.txt").write_text("tampered content\n", encoding="utf-8")
        docs = self._by_path(self.room.export_manifest("r1"))
        self.assertEqual(docs["docs/same.txt"]["status"], "ready")
        self.assertEqual(docs["other/same.txt"]["status"], "changed")

    def test_stored_values_and_filters(self):
        manifest = self.room.export_manifest("r1", tags=[" Legal ", "ops"])
        self.assertEqual([doc["path"] for doc in manifest["documents"]], ["notes.md"])
        note = manifest["documents"][0]
        self.assertEqual(note["tags"], ["legal", "ops"])
        self.assertEqual(note["bytes"], len("meeting notes\n"))
        self.assertEqual(note["status"], "ready")
        filtered = self.room.export_manifest("r1", text="SAME")
        self.assertEqual({doc["path"] for doc in filtered["documents"]},
                         {"docs/same.txt", "other/same.txt"})
        self.assertFalse(self.room.export_manifest("r1", tags=["legal", "ops", "finance"])["documents"])

    def test_complete_when_all_ready(self):
        ready_room = DocumentRoom(self.root, self.root / "ready.json")
        ready_room.add("notes.md")
        self.assertTrue(ready_room.export_manifest("r1")["complete"])

    def test_empty_result_has_all_fields_and_false_complete(self):
        manifest = self.room.export_manifest("r1", tags=["no-such-tag"])
        self.assertEqual(manifest, {"release": "r1", "complete": False, "documents": []})

    def test_missing_index_is_empty_and_not_created(self):
        room = DocumentRoom(self.root, self.root / "absent-index.json")
        self.assertEqual(room.export_manifest("r1"),
                         {"release": "r1", "complete": False, "documents": []})
        self.assertFalse((self.root / "absent-index.json").exists())

    def test_release_validation(self):
        for bad in (None, "", "   ", 123, b"r1", ["r1"]):
            with self.assertRaises(ValueError):
                self.room.export_manifest(bad)

    def test_directory_and_dangling_link_are_missing(self):
        (self.root / "notes.md").unlink()
        (self.root / "notes.md").mkdir()
        (self.root / "stale.md").unlink()
        os.symlink("does-not-exist", self.root / "stale.md")
        docs = self._by_path(self.room.export_manifest("r1", tags=["ops"]))
        self.assertEqual(docs["notes.md"]["status"], "missing")
        self.assertEqual(docs["stale.md"]["status"], "missing")

    def test_symlink_loop_is_unreadable(self):
        records = json.loads(self.index.read_text(encoding="utf-8"))
        records["docs/loop"] = {"path": "docs/loop", "name": "loop",
                                "bytes": 5, "sha256": sha256_text("loop"), "tags": ["ops"]}
        self.index.write_text(json.dumps(records), encoding="utf-8")
        (self.root / "docs" / "loop").symlink_to("loop")
        docs = self._by_path(self.room.export_manifest("r1", tags=["ops"]))
        self.assertEqual(docs["docs/loop"]["status"], "unreadable")

    def test_escape_is_unsafe_without_reading_target(self):
        outside = self.root.parent / "secret-outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        records = json.loads(self.index.read_text(encoding="utf-8"))
        records["../secret-outside.txt"] = {"path": "../secret-outside.txt", "name": "secret-outside.txt",
                                            "bytes": 7, "sha256": sha256_text("secret\n"), "tags": []}
        self.index.write_text(json.dumps(records), encoding="utf-8")
        doc = self._by_path(self.room.export_manifest("r1"))["../secret-outside.txt"]
        self.assertEqual(doc["status"], "unsafe")
        self.assertEqual(doc["path"], "../secret-outside.txt")

    def test_broken_and_malformed_index_raise_value_error(self):
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("r1")
        good_record = {"path": "a", "name": "a", "bytes": 1, "sha256": "x", "tags": []}
        for payload in ([good_record],
                        {"a": []},
                        {"a": {**good_record, "bytes": "1"}},
                        {"a": {**good_record, "sha256": 7}},
                        {"a": {**good_record, "tags": "ok"}},
                        {"a": {**good_record, "path": "b"}},
                        {"a": {**good_record, "extra": True}}):
            self.index.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.room.export_manifest("r1")

    def test_one_bad_file_does_not_stop_others(self):
        (self.root / "stale.md").unlink()
        os.symlink("does-not-exist", self.root / "stale.md")
        docs = self._by_path(self.room.export_manifest("r1", tags=["ops"]))
        self.assertEqual(docs["stale.md"]["status"], "missing")
        self.assertEqual(docs["other/same.txt"]["status"], "ready")
        self.assertEqual(docs["gone.md"]["status"], "missing")

    def test_cli_export(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        ok = subprocess.run(prefix + ["export", "--release", "  Ship  ", "--tag", "ops", "--text", "stale"],
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        manifest = json.loads(ok.stdout)
        self.assertEqual(manifest["release"], "Ship")
        self.assertFalse(manifest["complete"])
        self.assertEqual([d["path"] for d in manifest["documents"]], ["stale.md"])
        missing_release = subprocess.run(prefix + ["export"], capture_output=True, text=True)
        self.assertEqual(missing_release.returncode, 2)
        self.assertIn("error", json.loads(missing_release.stdout))
        self.index.write_text("broken", encoding="utf-8")
        broken = subprocess.run(prefix + ["export", "--release", "r1"], capture_output=True, text=True)
        self.assertEqual(broken.returncode, 2)
        self.assertIn("error", json.loads(broken.stdout))

    def test_cli_export_incomplete_is_still_zero(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        result = subprocess.run(prefix + ["export", "--release", "r1"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["complete"])


if __name__ == "__main__":
    unittest.main()
