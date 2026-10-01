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

    def test_manifest_ready_changed_and_missing(self):
        self.room.add("policy.md", [" Legal "])
        (self.root / "meeting.txt").write_text("Notes\n", encoding="utf-8")
        meeting = self.room.add("meeting.txt", ["operations"])
        manifest = self.room.export_manifest("  Q4 Release  ")
        self.assertEqual(manifest["release"], "Q4 Release")
        self.assertTrue(manifest["complete"])
        by_path = {d["path"]: d for d in manifest["documents"]}
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertEqual(by_path["policy.md"]["status"], "ready")
        # A modified file is "changed"; stored metadata is preserved, not overwritten.
        original = by_path["policy.md"]
        (self.root / "policy.md").write_text("Totally different policy body", encoding="utf-8")
        manifest = self.room.export_manifest("Q4 Release")
        changed = {d["path"]: d for d in manifest["documents"]}["policy.md"]
        self.assertEqual(changed["status"], "changed")
        self.assertEqual(changed["bytes"], original["bytes"])
        self.assertEqual(changed["sha256"], original["sha256"])
        self.assertFalse(manifest["complete"])
        # A removed file is "missing".
        (self.root / "meeting.txt").unlink()
        self.assertEqual({d["path"]: d["status"] for d in self.room.export_manifest("r")["documents"]},
                         {"meeting.txt": "missing", "policy.md": "changed"})
        self.assertEqual(meeting["tags"], ["operations"])

    def test_same_named_files_judged_independently(self):
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        (self.root / "a" / "doc.txt").write_text("version one", encoding="utf-8")
        (self.root / "b" / "doc.txt").write_text("version two", encoding="utf-8")
        self.room.add("a/doc.txt", ["legal"])
        self.room.add("b/doc.txt", ["legal"])
        (self.root / "b" / "doc.txt").write_text("version two changed", encoding="utf-8")
        statuses = {d["path"]: d["status"]
                    for d in self.room.export_manifest("r", ["legal"])["documents"]}
        self.assertEqual(statuses, {"a/doc.txt": "ready", "b/doc.txt": "changed"})

    def test_manifest_empty_without_index(self):
        fresh = DocumentRoom(self.root / "nested", self.root / "fresh.json")
        manifest = fresh.export_manifest("r", ["legal"], "nope")
        self.assertEqual(set(manifest), {"release", "complete", "documents"})
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["documents"], [])
        self.assertFalse((self.root / "fresh.json").exists())

    def test_manifest_release_validation(self):
        for bad in ("", "   ", None, 7, ["r"]):
            with self.assertRaises(ValueError):
                self.room.export_manifest(bad)

    def test_unsafe_symlink_is_not_read(self):
        self.room.add("policy.md")
        outside = self.root.parent / "outside-secret.txt"
        outside.write_text("outside root", encoding="utf-8")
        self.addCleanup(outside.unlink)
        link = self.root / "policy.md"
        link.unlink()
        link.symlink_to(outside)
        document = self.room.export_manifest("r")["documents"][0]
        self.assertEqual(document["status"], "unsafe")
        # A dangling symlink is "missing".
        link.unlink()
        link.symlink_to(self.root / "gone.txt")
        self.assertEqual(self.room.export_manifest("r")["documents"][0]["status"], "missing")
        # A directory where a file was registered is "missing" too.
        link.unlink()
        link.mkdir()
        self.assertEqual(self.room.export_manifest("r")["documents"][0]["status"], "missing")

    def test_corrupt_or_malformed_index_raises(self):
        self.room.add("policy.md")
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("r")
        self.index.write_text(json.dumps({"policy.md": {"path": "elsewhere.md", "name": "p",
                                                        "bytes": 1, "sha256": "x", "tags": []}}),
                              encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("r")
        self.index.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("r")
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.export_manifest("r")
        finally:
            os.chmod(self.index, 0o644)

    def test_cli_export_exit_codes_and_payload(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        self.room.add("policy.md", ["legal"])
        (self.root / "policy.md").write_text("changed on disk", encoding="utf-8")
        # Incomplete manifests still succeed with exit 0.
        result = subprocess.run(prefix + ["export", "--release", " R1 ", "--tag", "LEGAL", "--text", "POLICY"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["release"], "R1")
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["documents"][0]["status"], "changed")
        # Missing/blank release is a JSON error with exit 2.
        result = subprocess.run(prefix + ["export"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        result = subprocess.run(prefix + ["export", "--release", "   "], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))

    def _add_policy_and_meeting(self):
        self.room.add("policy.md", ["legal"])
        (self.root / "meeting.txt").write_text("Notes\n", encoding="utf-8")
        self.room.add("meeting.txt", ["operations"])

    def test_set_references_roundtrip_and_search(self):
        self._add_policy_and_meeting()
        record = self.room.set_references("policy.md", ["meeting.txt", "meeting.txt"])
        self.assertEqual(record["references"], ["meeting.txt"])
        self.assertEqual(record["path"], "policy.md")
        found = self.room.search(["legal"])
        self.assertEqual(found[0]["references"], ["meeting.txt"])
        # Omitting targets clears the references but keeps the empty list.
        record = self.room.set_references("policy.md", [])
        self.assertEqual(record["references"], [])
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["policy.md"]["references"], [])

    def test_self_and_mutual_references_allowed(self):
        self._add_policy_and_meeting()
        self.assertEqual(self.room.set_references("policy.md", ["policy.md"])["references"],
                         ["policy.md"])
        self.room.set_references("policy.md", ["meeting.txt"])
        self.room.set_references("meeting.txt", ["policy.md"])
        manifest = self.room.export_manifest("r", ["legal"], include_references=True)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertTrue(manifest["complete"])

    def test_set_references_validation(self):
        self._add_policy_and_meeting()
        before = self.index.read_text(encoding="utf-8")
        for source, targets in ((None, []), (7, []), ("absent.md", []),
                                ("policy.md", "meeting.txt"),
                                ("policy.md", ["meeting.txt", 3]),
                                ("policy.md", ["absent.md"])):
            with self.assertRaises(ValueError):
                self.room.set_references(source, targets)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)
        # A missing index is an empty index: the source is unregistered and
        # the index is not created.
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        with self.assertRaises(ValueError):
            fresh.set_references("policy.md", [])
        self.assertFalse((self.root / "fresh.json").exists())

    def test_set_references_index_integrity(self):
        self._add_policy_and_meeting()
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        self.index.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        self.index.unlink()
        self.room.add("policy.md")
        self.room.add("meeting.txt")
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        stored["policy.md"]["references"] = "meeting.txt"
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        stored["policy.md"]["references"] = ["absent.md"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.set_references("policy.md", [])
        finally:
            os.chmod(self.index, 0o644)

    def test_add_preserves_references_on_refresh(self):
        self._add_policy_and_meeting()
        first = self.room.add("policy.md")
        self.assertNotIn("references", first)
        self.room.set_references("policy.md", ["meeting.txt"])
        (self.root / "policy.md").write_text("New policy body\n", encoding="utf-8")
        refreshed = self.room.add("policy.md", ["updated"])
        self.assertEqual(refreshed["references"], ["meeting.txt"])
        self.assertEqual(refreshed["tags"], ["updated"])

    def test_manifest_with_references_expands_and_checks(self):
        self._add_policy_and_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        # Filtering to policy.md still pulls in the referenced meeting.txt.
        manifest = self.room.export_manifest("r", ["legal"], "policy",
                                             include_references=True)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertTrue(manifest["complete"])
        # Without the flag the same filters keep only policy.md.
        manifest = self.room.export_manifest("r", ["legal"], "policy")
        self.assertEqual([d["path"] for d in manifest["documents"]], ["policy.md"])
        # A modified referenced document is changed and breaks completeness.
        (self.root / "meeting.txt").write_text("Updated notes\n", encoding="utf-8")
        manifest = self.room.export_manifest("r", ["legal"], "policy",
                                             include_references=True)
        by_path = {d["path"]: d for d in manifest["documents"]}
        self.assertEqual(by_path["meeting.txt"]["status"], "changed")
        self.assertEqual(by_path["policy.md"]["status"], "ready")
        self.assertFalse(manifest["complete"])
        # Exporting is read-only: the stored metadata is untouched.
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["meeting.txt"]["bytes"], len("Notes\n"))

    def test_manifest_with_references_validates_index(self):
        self._add_policy_and_meeting()
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        stored["policy.md"]["references"] = ["absent.md"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("r", include_references=True)
        # Records without a references field are treated as having none.
        del stored["policy.md"]["references"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        manifest = self.room.export_manifest("r", include_references=True)
        self.assertEqual(len(manifest["documents"]), 2)

    def test_cli_refs_and_export_with_references(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        self._add_policy_and_meeting()
        result = subprocess.run(prefix + ["refs", "policy.md", "--to", "meeting.txt"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["references"], ["meeting.txt"])
        result = subprocess.run(prefix + ["export", "--release", "R1", "--text", "policy",
                                          "--with-references"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([d["path"] for d in json.loads(result.stdout)["documents"]],
                         ["meeting.txt", "policy.md"])
        # Omitting --to clears the references.
        result = subprocess.run(prefix + ["refs", "policy.md"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["references"], [])
        # Unknown source or target is a JSON error with exit 2.
        result = subprocess.run(prefix + ["refs", "policy.md", "--to", "absent.md"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
