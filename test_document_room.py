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

    def _add_meeting(self):
        (self.root / "meeting.txt").write_text("Notes\n", encoding="utf-8")
        return self.room.add("meeting.txt", ["operations"])

    def test_set_references_roundtrip_dedup_sort_and_clear(self):
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        record = self.room.set_references("policy.md", ["meeting.txt", "meeting.txt", "policy.md"])
        self.assertEqual(record["references"], ["meeting.txt", "policy.md"])
        # The stored record keeps its original fields plus the references.
        self.assertEqual(record["tags"], ["legal"])
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["policy.md"]["references"], ["meeting.txt", "policy.md"])
        # Mutual references are allowed.
        back = self.room.set_references("meeting.txt", ["policy.md"])
        self.assertEqual(back["references"], ["policy.md"])
        # An empty target list clears the references but keeps the field.
        cleared = self.room.set_references("policy.md", [])
        self.assertEqual(cleared["references"], [])
        self.assertIn("references", json.loads(self.index.read_text(encoding="utf-8"))["policy.md"])

    def test_set_references_validation_leaves_index_untouched(self):
        self.room.add("policy.md")
        self._add_meeting()
        before = self.index.read_text(encoding="utf-8")
        for args in ((None, []), (7, []), ("policy.md", "meeting.txt"),
                     ("policy.md", ["meeting.txt", 3]), ("policy.md", [None]),
                     ("missing.md", []), ("POLICY.MD", []),
                     ("policy.md", ["missing.md"])):
            with self.assertRaises(ValueError, msg=repr(args)):
                self.room.set_references(*args)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_set_references_without_index_raises_and_creates_nothing(self):
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        with self.assertRaises(ValueError):
            fresh.set_references("policy.md", [])
        self.assertFalse((self.root / "fresh.json").exists())

    def test_set_references_rejects_corrupt_index_and_bad_references(self):
        self.room.add("policy.md")
        self._add_meeting()
        valid = self.index.read_text(encoding="utf-8")
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        self.index.write_text(valid, encoding="utf-8")
        self.room.set_references("policy.md", ["meeting.txt"])
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        # A stored references field that is not a list of strings is rejected.
        stored["policy.md"]["references"] = "meeting.txt"
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        # A stored reference to an unregistered path is rejected.
        stored["policy.md"]["references"] = ["ghost.txt"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_references("policy.md", [])
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.set_references("policy.md", [])
        finally:
            os.chmod(self.index, 0o644)

    def test_add_preserves_references_and_search_returns_them(self):
        first = self.room.add("policy.md", ["legal"])
        self.assertNotIn("references", first)
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        (self.root / "policy.md").write_text("Revised policy\n", encoding="utf-8")
        again = self.room.add("policy.md", ["updated"])
        self.assertEqual(again["references"], ["meeting.txt"])
        self.assertEqual(again["tags"], ["updated"])
        found = self.room.search(["updated"])
        self.assertEqual(found[0]["references"], ["meeting.txt"])
        # Records without references are returned as stored.
        self.assertNotIn("references", self.room.search(["operations"])[0])

    def test_manifest_with_references_expands_and_detects_changes(self):
        self.room.add("policy.md", ["legal"])
        meeting = self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        # Filtering to policy.md still pulls in the referenced meeting.txt,
        # even though meeting.txt does not match the tag filter.
        manifest = self.room.export_manifest("R1", ["legal"], include_references=True)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertTrue(manifest["complete"])
        self.assertEqual(set(manifest["documents"][0]),
                         {"path", "name", "bytes", "sha256", "tags", "status"})
        # Without the flag the filter keeps meeting.txt out.
        plain = self.room.export_manifest("R1", ["legal"])
        self.assertEqual([d["path"] for d in plain["documents"]], ["policy.md"])
        # A changed referenced file breaks completeness.
        (self.root / "meeting.txt").write_text("Revised notes\n", encoding="utf-8")
        manifest = self.room.export_manifest("R1", ["legal"], include_references=True)
        by_path = {d["path"]: d for d in manifest["documents"]}
        self.assertEqual(by_path["meeting.txt"]["status"], "changed")
        self.assertEqual(by_path["meeting.txt"]["bytes"], meeting["bytes"])
        self.assertFalse(manifest["complete"])
        # Exporting is read-only: the index still holds the old metadata.
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["meeting.txt"]["bytes"], meeting["bytes"])

    def test_manifest_with_references_cycles_and_transitive_closure(self):
        self.room.add("policy.md")
        self._add_meeting()
        (self.root / "notes.md").write_text("More\n", encoding="utf-8")
        self.room.add("notes.md")
        # A cycle plus a transitive chain still terminates without duplicates.
        self.room.set_references("policy.md", ["meeting.txt"])
        self.room.set_references("meeting.txt", ["policy.md", "notes.md"])
        self.room.set_references("notes.md", ["notes.md"])
        manifest = self.room.export_manifest("R1", text="policy", include_references=True)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "notes.md", "policy.md"])
        self.assertTrue(manifest["complete"])

    def test_manifest_with_references_validates_index(self):
        self.room.add("policy.md")
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        stored["policy.md"]["references"] = ["ghost.txt"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("R1", include_references=True)
        # The default export keeps the original behaviour and ignores it.
        manifest = self.room.export_manifest("R1")
        self.assertEqual(len(manifest["documents"]), 2)

    def test_reference_impact_direct_and_transitive_distances(self):
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        (self.root / "notes.md").write_text("More\n", encoding="utf-8")
        self.room.add("notes.md")
        # a -> b -> c and d -> c
        self.room.set_references("policy.md", ["meeting.txt"])
        self.room.set_references("meeting.txt", ["notes.md"])
        (self.root / "extra.txt").write_text("x\n", encoding="utf-8")
        self.room.add("extra.txt")
        self.room.set_references("extra.txt", ["notes.md"])
        direct = self.room.reference_impact("notes.md")
        self.assertEqual(direct["path"], "notes.md")
        self.assertEqual([(d["path"], d["distance"]) for d in direct["documents"]],
                         [("extra.txt", 1), ("meeting.txt", 1)])
        transitive = self.room.reference_impact("notes.md", transitive=True)
        self.assertEqual([(d["path"], d["distance"]) for d in transitive["documents"]],
                         [("extra.txt", 1), ("meeting.txt", 1), ("policy.md", 2)])
        self.assertTrue(all(d["distance"] == 1 for d in direct["documents"]))
        # Each item is exactly the search record plus an integer distance.
        by_path = {r["path"]: r for r in self.room.search()}
        for document in transitive["documents"]:
            distance = document.pop("distance")
            self.assertIsInstance(distance, int)
            self.assertEqual(document, by_path[document["path"]])

    def test_reference_impact_excludes_target_self_reference_and_cycles(self):
        self.room.add("policy.md")
        self._add_meeting()
        # Self-reference and a mutual cycle stay legal and terminate.
        self.room.set_references("policy.md", ["meeting.txt", "policy.md"])
        self.room.set_references("meeting.txt", ["policy.md"])
        result = self.room.reference_impact("policy.md", transitive=True)
        self.assertEqual([d["path"] for d in result["documents"]], ["meeting.txt"])
        self.assertEqual(result["documents"][0]["distance"], 1)
        # An existing document that nobody references yields an empty result.
        (self.root / "lonely.txt").write_text("solo\n", encoding="utf-8")
        self.room.add("lonely.txt")
        self.assertEqual(self.room.reference_impact("lonely.txt", transitive=True)["documents"], [])

    def test_reference_impact_reads_index_only(self):
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        before = self.index.read_text(encoding="utf-8")
        # Missing/changed source files do not affect the stored-graph result.
        (self.root / "policy.md").unlink()
        result = self.room.reference_impact("meeting.txt")
        self.assertEqual([d["path"] for d in result["documents"]], ["policy.md"])
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_reference_impact_validation(self):
        self.room.add("policy.md")
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        for bad in (None, 7, ["meeting.txt"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.reference_impact(bad)
        with self.assertRaises(ValueError):
            self.room.reference_impact("meeting.txt", transitive="yes")
        with self.assertRaises(ValueError):
            self.room.reference_impact("MEETING.TXT")  # exact, case-sensitive key
        with self.assertRaises(ValueError):
            self.room.reference_impact("ghost.md")
        # Missing index: empty index, unregistered target, and no file created.
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        with self.assertRaises(ValueError):
            fresh.reference_impact("meeting.txt")
        self.assertFalse((self.root / "fresh.json").exists())

    def test_reference_impact_rejects_corrupt_index_even_unrelated(self):
        self.room.add("policy.md")
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        valid = self.index.read_text(encoding="utf-8")
        # A dangling reference on an unrelated record still invalidates the query.
        stored = json.loads(valid)
        stored["policy.md"]["references"] = ["ghost.txt"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.reference_impact("meeting.txt")
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.reference_impact("meeting.txt")
        # An old record without references is treated as having none.
        stored = json.loads(valid)
        del stored["policy.md"]["references"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual(self.room.reference_impact("meeting.txt")["documents"], [])
        self.assertNotIn("references", json.loads(self.index.read_text(encoding="utf-8"))["policy.md"])
        self.index.write_text(valid, encoding="utf-8")
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.reference_impact("meeting.txt")
        finally:
            os.chmod(self.index, 0o644)

    def test_cli_impact_exit_codes_and_payload(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        result = subprocess.run(prefix + ["impact", "meeting.txt"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["path"], "meeting.txt")
        self.assertEqual([d["path"] for d in payload["documents"]], ["policy.md"])
        self.assertEqual(payload["documents"][0]["distance"], 1)
        result = subprocess.run(prefix + ["impact", "policy.md", "--transitive"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["documents"], [])
        result = subprocess.run(prefix + ["impact", "ghost.md"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))

    def test_cli_refs_and_export_with_references(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        result = subprocess.run(prefix + ["refs", "policy.md", "--to", "meeting.txt"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["references"], ["meeting.txt"])
        result = subprocess.run(prefix + ["export", "--release", "R1", "--tag", "legal",
                                          "--with-references"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertTrue(manifest["complete"])
        # Omitting --to clears the references.
        result = subprocess.run(prefix + ["refs", "policy.md"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["references"], [])
        # Unknown source or target is a JSON error with exit 2.
        for bad in (["refs", "ghost.md"], ["refs", "policy.md", "--to", "ghost.md"]):
            result = subprocess.run(prefix + bad, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
