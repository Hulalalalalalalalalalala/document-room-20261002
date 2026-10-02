import json
import hashlib
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

    def _add_document(self, name, tags=()):
        (self.root / name).write_text(f"Content of {name}\n", encoding="utf-8")
        return self.room.add(name, tags)

    def test_manifest_with_origins_shared_sources_and_distances(self):
        self._add_document("a.md", ["release"])
        self._add_document("b.md", ["release"])
        self._add_document("c.md")
        self._add_document("d.md")
        self.room.set_references("a.md", ["c.md"])
        self.room.set_references("b.md", ["d.md"])
        self.room.set_references("d.md", ["c.md"])
        manifest = self.room.export_manifest("R1", ["release"], include_references=True,
                                             include_origins=True)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["a.md", "b.md", "c.md", "d.md"])
        by_path = {d["path"]: d for d in manifest["documents"]}
        self.assertEqual(by_path["a.md"]["origins"], [{"path": "a.md", "distance": 0}])
        self.assertEqual(by_path["b.md"]["origins"], [{"path": "b.md", "distance": 0}])
        # The shared document keeps every starting source at its shortest distance.
        self.assertEqual(by_path["c.md"]["origins"],
                         [{"path": "a.md", "distance": 1},
                          {"path": "b.md", "distance": 2}])
        self.assertEqual(by_path["d.md"]["origins"], [{"path": "b.md", "distance": 1}])
        # A back-reference extends the reach of other starting documents.
        self.room.set_references("c.md", ["a.md"])
        manifest = self.room.export_manifest("R1", ["release"], include_references=True,
                                             include_origins=True)
        by_path = {d["path"]: d for d in manifest["documents"]}
        self.assertEqual(by_path["a.md"]["origins"],
                         [{"path": "a.md", "distance": 0},
                          {"path": "b.md", "distance": 3}])
        self.assertEqual(by_path["b.md"]["origins"], [{"path": "b.md", "distance": 0}])
        self.assertEqual(by_path["c.md"]["origins"],
                         [{"path": "a.md", "distance": 1},
                          {"path": "b.md", "distance": 2}])

    def test_manifest_with_origins_cycles_and_self_reference(self):
        self._add_document("a.md", ["release"])
        self._add_document("b.md")
        self.room.set_references("a.md", ["a.md", "b.md"])
        self.room.set_references("b.md", ["a.md"])
        manifest = self.room.export_manifest("R1", ["release"], include_references=True,
                                             include_origins=True)
        by_path = {d["path"]: d for d in manifest["documents"]}
        # The starting document keeps distance 0 despite its self-reference.
        self.assertEqual(by_path["a.md"]["origins"], [{"path": "a.md", "distance": 0}])
        self.assertEqual(by_path["b.md"]["origins"], [{"path": "a.md", "distance": 1}])

    def test_manifest_with_origins_requires_references_and_boolean(self):
        self._add_document("a.md", ["release"])
        with self.assertRaises(ValueError):
            self.room.export_manifest("R1", include_origins=True)
        for bad in (1, "yes", None):
            with self.assertRaises(ValueError):
                self.room.export_manifest("R1", include_references=True,
                                          include_origins=bad)
        # Without the new flag the manifest carries no origins.
        manifest = self.room.export_manifest("R1", ["release"], include_references=True)
        self.assertNotIn("origins", manifest["documents"][0])

    def test_manifest_with_origins_validates_whole_index(self):
        self._add_document("a.md", ["release"])
        self._add_document("b.md")
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        # The corrupt record is not selected, yet origins export still fails.
        stored["b.md"]["references"] = ["ghost.md"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_manifest("R1", ["release"], include_references=True,
                                      include_origins=True)
        # An empty selection still yields an empty, incomplete manifest.
        del stored["b.md"]["references"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        empty = self.room.export_manifest("R1", ["nothing"], include_references=True,
                                          include_origins=True)
        self.assertEqual(empty["documents"], [])
        self.assertFalse(empty["complete"])

    def test_cli_export_with_origins(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root), "--index", str(self.index)]
        self._add_document("a.md", ["release"])
        self._add_document("b.md")
        self.room.set_references("a.md", ["b.md"])
        result = subprocess.run(prefix + ["export", "--release", "R1", "--tag", "release",
                                          "--with-references", "--with-origins"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        by_path = {d["path"]: d for d in manifest["documents"]}
        self.assertEqual(by_path["b.md"]["origins"], [{"path": "a.md", "distance": 1}])
        # Origins without reference expansion is a JSON error with exit 2.
        result = subprocess.run(prefix + ["export", "--release", "R1", "--with-origins"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))

    def test_compare_preserves_origins_without_comparing_them(self):
        self._add_document("a.md", ["release"])
        self._add_document("b.md")
        self.room.set_references("a.md", ["b.md"])
        before = self.room.export_manifest("R1", ["release"], include_references=True,
                                           include_origins=True)
        after = self.room.export_manifest("R1", ["release"], include_references=True,
                                          include_origins=True)
        after["documents"][1]["origins"] = [{"path": "b.md", "distance": 0}]
        result = DocumentRoom.compare_manifests(before, after)
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["unchanged"], ["a.md", "b.md"])
        # The extra field survives in the returned documents.
        added = dict(before)
        added["documents"] = before["documents"] + [dict(after["documents"][1],
                                                         path="extra.md")]
        result = DocumentRoom.compare_manifests(before, added)
        self.assertEqual(result["added"][0]["origins"], [{"path": "b.md", "distance": 0}])

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

    def test_find_duplicates_groups_by_bytes_and_sha256(self):
        (self.root / "a.txt").write_text("same body\n", encoding="utf-8")
        (self.root / "b.txt").write_text("same body\n", encoding="utf-8")
        (self.root / "c.txt").write_text("different body\n", encoding="utf-8")
        self.room.add("a.txt", ["release"])
        self.room.add("b.txt")
        self.room.add("c.txt")
        result = self.room.find_duplicates()
        self.assertEqual(set(result), {"groups"})
        (group,) = result["groups"]
        self.assertEqual(set(group), {"bytes", "sha256", "matched", "documents"})
        self.assertEqual(group["bytes"], 10)
        self.assertEqual(group["sha256"], hashlib.sha256(b"same body\n").hexdigest())
        self.assertEqual(group["matched"], ["a.txt", "b.txt"])
        self.assertEqual([d["path"] for d in group["documents"]], ["a.txt", "b.txt"])

    def test_find_duplicates_filter_returns_whole_group(self):
        (self.root / "a.txt").write_text("same body\n", encoding="utf-8")
        (self.root / "b.txt").write_text("same body\n", encoding="utf-8")
        first = self.room.add("a.txt", ["release"])
        second = self.room.add("b.txt", ["draft"])
        # Filtering by a tag only a.txt carries still returns the whole group,
        # but matched lists only the member that matched.
        group = self.room.find_duplicates([" RELEASE "])["groups"][0]
        self.assertEqual(group["matched"], ["a.txt"])
        self.assertEqual(group["documents"], [first, second])
        self.assertEqual(
            self.room.find_duplicates(text="B.TXT")["groups"][0]["matched"], ["b.txt"])
        # A tag no member carries yields nothing.
        self.assertEqual(self.room.find_duplicates(["finance"]), {"groups": []})
        # Intersection of tags is required on the matching member.
        self.assertEqual(self.room.find_duplicates(["release", "draft"]), {"groups": []})

    def test_find_duplicates_requires_both_bytes_and_sha256(self):
        # Same size or same digest alone never groups records; name is ignored.
        records = [
            {"path": "one.txt", "name": "one.txt", "bytes": 5, "sha256": "h1", "tags": []},
            {"path": "two.txt", "name": "two.txt", "bytes": 5, "sha256": "h2", "tags": []},
            {"path": "three.txt", "name": "one.txt", "bytes": 6, "sha256": "h1", "tags": []},
        ]
        self.index.write_text(json.dumps({r["path"]: r for r in records}), encoding="utf-8")
        self.assertEqual(self.room.find_duplicates(), {"groups": []})
        # A second identical copy forms one group with the first.
        records.append({"path": "mid.txt", "name": "mid.txt", "bytes": 5, "sha256": "h1", "tags": []})
        self.index.write_text(json.dumps({r["path"]: r for r in records}), encoding="utf-8")
        group = self.room.find_duplicates()["groups"][0]
        self.assertEqual([d["path"] for d in group["documents"]], ["mid.txt", "one.txt"])
        self.assertEqual(group["matched"], ["mid.txt", "one.txt"])

    def test_find_duplicates_sorts_groups_by_smallest_member(self):
        groups_data = [
            ("z1.txt", "z2.txt", "zz"), ("a1.txt", "a2.txt", "aa"),
            ("m1.txt", "m2.txt", "mm"),
        ]
        for first, second, body in groups_data:
            (self.root / first).write_text(body, encoding="utf-8")
            (self.root / second).write_text(body, encoding="utf-8")
            self.room.add(first)
            self.room.add(second)
        result = self.room.find_duplicates()
        self.assertEqual([g["documents"][0]["path"] for g in result["groups"]],
                         ["a1.txt", "m1.txt", "z1.txt"])
        # A text filter that only hits a member of one group returns just it.
        result = self.room.find_duplicates(text="M2")
        self.assertEqual([d["path"] for d in result["groups"][0]["documents"]],
                         ["m1.txt", "m2.txt"])

    def test_find_duplicates_keeps_stored_fields_and_reads_index_only(self):
        (self.root / "a.txt").write_text("same body\n", encoding="utf-8")
        (self.root / "b.txt").write_text("same body\n", encoding="utf-8")
        self.room.add("a.txt", ["release"])
        self.room.add("b.txt")
        self.room.set_references("a.txt", ["b.txt"])
        before = self.index.read_text(encoding="utf-8")
        # The stored references field is passed through; no status field is added.
        group = self.room.find_duplicates()["groups"][0]
        self.assertEqual(group["documents"][0]["references"], ["b.txt"])
        self.assertNotIn("status", group["documents"][0])
        self.assertNotIn("references", group["documents"][1])
        # Changing or removing live files does not change the index-based result.
        expected = self.room.find_duplicates()
        (self.root / "a.txt").write_text("totally changed now", encoding="utf-8")
        (self.root / "b.txt").unlink()
        self.assertEqual(self.room.find_duplicates(), expected)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_find_duplicates_without_index_returns_empty_and_creates_nothing(self):
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        self.assertEqual(fresh.find_duplicates(), {"groups": []})
        self.assertFalse((self.root / "fresh.json").exists())

    def test_find_duplicates_validation(self):
        self.room.add("policy.md")
        for bad in (None, 7, "legal", {"x"}, [1], [None]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.find_duplicates(bad)
        for bad in (None, 7, ["x"], b"x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.find_duplicates(text=bad)

    def test_find_duplicates_rejects_corrupt_index_even_unrelated(self):
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        self.room.add("a.txt")
        self.room.add("b.txt")
        valid = self.index.read_text(encoding="utf-8")
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.find_duplicates()
        # A malformed record never matched by a filter still invalidates the query.
        stored = json.loads(valid)
        stored["a.txt"]["sha256"] = 5
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.find_duplicates(text="zzz")
        # A dangling reference also invalidates the whole index.
        stored = json.loads(valid)
        stored["a.txt"]["references"] = ["ghost.txt"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.find_duplicates()
        # Old records without references stay legal.
        self.index.write_text(valid, encoding="utf-8")
        self.assertEqual(len(self.room.find_duplicates()["groups"]), 1)
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.find_duplicates()
        finally:
            os.chmod(self.index, 0o644)

    def test_find_duplicates_does_not_mutate_records(self):
        (self.root / "a.txt").write_text("same body\n", encoding="utf-8")
        (self.root / "b.txt").write_text("same body\n", encoding="utf-8")
        self.room.add("a.txt", ["release"])
        self.room.add("b.txt")
        snapshot = self.index.read_text(encoding="utf-8")
        self.room.find_duplicates(["release"], "a")
        self.assertEqual(self.index.read_text(encoding="utf-8"), snapshot)

    def test_cli_duplicates_exit_codes_and_payload(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root),
                  "--index", str(self.index)]
        (self.root / "a.txt").write_text("same body\n", encoding="utf-8")
        (self.root / "b.txt").write_text("same body\n", encoding="utf-8")
        self.room.add("a.txt", ["release"])
        self.room.add("b.txt")
        result = subprocess.run(prefix + ["duplicates", "--tag", "RELEASE", "--text", "a.txt"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(result.stdout)["groups"][0]
        self.assertEqual(group["matched"], ["a.txt"])
        self.assertEqual([d["path"] for d in group["documents"]], ["a.txt", "b.txt"])
        # No duplicates: empty groups payload, still exit 0.
        (self.root / "b.txt").write_text("different", encoding="utf-8")
        self.room.add("b.txt")
        result = subprocess.run(prefix + ["duplicates"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"groups": []})
        # A corrupt index is reported as an error payload with exit 2.
        self.index.write_text("{not json", encoding="utf-8")
        result = subprocess.run(prefix + ["duplicates"], capture_output=True, text=True)
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

    def test_set_category_roundtrip_and_preserves_record(self):
        self.room.add("policy.md", ["legal"])
        meeting = self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        self.room.set_archived("meeting.txt", True)
        # Surrounding whitespace is stripped; case and inner whitespace stay.
        record = self.room.set_category("policy.md", "  Legal  Docs ")
        self.assertEqual(record["category"], "Legal  Docs")
        # Other metadata, references and archive state are untouched.
        self.assertEqual(record["tags"], ["legal"])
        self.assertEqual(record["references"], ["meeting.txt"])
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["policy.md"]["category"], "Legal  Docs")
        self.assertEqual(stored["meeting.txt"]["archived"], True)
        self.assertNotIn("category", stored["meeting.txt"])
        # Setting the same category twice returns the identical record.
        self.assertEqual(self.room.set_category("policy.md", "Legal  Docs"), record)
        # An empty string marks the document uncategorized, stored explicitly.
        cleared = self.room.set_category("policy.md", "   ")
        self.assertEqual(cleared["category"], "")
        self.assertIn("category", json.loads(self.index.read_text(encoding="utf-8"))["policy.md"])
        self.assertEqual(meeting["tags"], ["operations"])

    def test_set_category_without_index_raises_and_creates_nothing(self):
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        with self.assertRaises(ValueError):
            fresh.set_category("policy.md", "legal")
        self.assertFalse((self.root / "fresh.json").exists())

    def test_set_category_validation_leaves_index_untouched(self):
        self.room.add("policy.md")
        before = self.index.read_text(encoding="utf-8")
        for args in ((None, "legal"), (7, "legal"), ("policy.md", None),
                     ("policy.md", 7), ("policy.md", ["legal"]), ("policy.md", True),
                     ("missing.md", "legal"), ("POLICY.MD", "legal")):
            with self.assertRaises(ValueError, msg=repr(args)):
                self.room.set_category(*args)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_set_category_validates_whole_index(self):
        self.room.add("policy.md")
        self._add_meeting()
        valid = self.index.read_text(encoding="utf-8")
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_category("policy.md", "legal")
        # A non-string category on another record invalidates the whole index.
        stored = json.loads(valid)
        stored["meeting.txt"]["category"] = 7
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_category("policy.md", "legal")
        # A dangling reference on an unrelated record is also rejected.
        stored = json.loads(valid)
        stored["meeting.txt"]["references"] = ["ghost.txt"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.set_category("policy.md", "legal")
        self.index.write_text(valid, encoding="utf-8")
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.set_category("policy.md", "legal")
        finally:
            os.chmod(self.index, 0o644)

    def test_add_preserves_category(self):
        first = self.room.add("policy.md", ["legal"])
        self.assertNotIn("category", first)
        self.room.set_category("policy.md", "Legal")
        (self.root / "policy.md").write_text("Revised policy\n", encoding="utf-8")
        again = self.room.add("policy.md", ["updated"])
        self.assertEqual(again["category"], "Legal")
        self.assertEqual(again["tags"], ["updated"])
        self.assertEqual(self.room.search(["updated"])[0]["category"], "Legal")

    def test_search_category_filter(self):
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        (self.root / "notes.md").write_text("More\n", encoding="utf-8")
        self.room.add("notes.md", ["legal"])
        self.room.set_category("policy.md", "Release")
        self.room.set_category("meeting.txt", "release")  # case differs: no match
        # None (or omitting the argument) disables the category filter.
        self.assertEqual(len(self.room.search()), 3)
        self.assertEqual(len(self.room.search(category=None)), 3)
        # The filter value is stripped, then compared case-sensitively in full.
        self.assertEqual([r["path"] for r in self.room.search(category=" Release ")],
                         ["policy.md"])
        self.assertEqual([r["path"] for r in self.room.search(category="release")],
                         ["meeting.txt"])
        # An explicit empty string selects only uncategorized records.
        self.assertEqual([r["path"] for r in self.room.search(category="")], ["notes.md"])
        self.assertEqual([r["path"] for r in self.room.search(category="  ")], ["notes.md"])
        # Category intersects with the tag, text and archive filters.
        self.assertEqual([r["path"] for r in self.room.search(["legal"], category="Release")],
                         ["policy.md"])
        self.assertEqual(self.room.search(["legal"], text="notes", category="Release"), [])
        self.room.set_archived("policy.md", True)
        self.assertEqual(self.room.search(category="Release", archive_state="active"), [])
        self.assertEqual([r["path"] for r in self.room.search(category="Release",
                                                               archive_state="archived")],
                         ["policy.md"])

    def test_category_filter_requires_string_or_none(self):
        self.room.add("policy.md")
        for bad in (7, True, ["legal"], b"legal"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.search(category=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.export_manifest("r", category=bad)

    def test_category_filter_validates_whole_index(self):
        self.room.add("policy.md")
        self._add_meeting()
        self.room.set_category("policy.md", "legal")
        valid = self.index.read_text(encoding="utf-8")
        # A non-string category on a record the filter would not select still fails.
        stored = json.loads(valid)
        stored["meeting.txt"]["category"] = ["ops"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.search(category="legal")
        with self.assertRaises(ValueError):
            self.room.export_manifest("r", category="legal")
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.search(category="legal")
        # Old records without a category field stay legal and uncategorized.
        self.index.write_text(valid, encoding="utf-8")
        self.assertEqual([r["path"] for r in self.room.search(category="")],
                         ["meeting.txt"])
        self.assertNotIn("category", json.loads(self.index.read_text(encoding="utf-8"))["meeting.txt"])

    def test_category_filter_without_index_returns_empty_and_creates_nothing(self):
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        self.assertEqual(fresh.search(category="legal"), [])
        manifest = fresh.export_manifest("r", category="legal")
        self.assertEqual(manifest["documents"], [])
        self.assertFalse(manifest["complete"])
        self.assertFalse((self.root / "fresh.json").exists())

    def test_export_category_filter_and_cross_category_references(self):
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        self.room.set_category("policy.md", "legal")
        self.room.set_category("meeting.txt", "operations")
        self.room.set_references("policy.md", ["meeting.txt"])
        # The plain category filter keeps only the matching document.
        plain = self.room.export_manifest("R1", category="legal")
        self.assertEqual([d["path"] for d in plain["documents"]], ["policy.md"])
        self.assertEqual(set(plain["documents"][0]),
                         {"path", "name", "bytes", "sha256", "tags", "status"})
        # Reference expansion pulls in the referenced document across categories.
        expanded = self.room.export_manifest("R1", category="legal", include_references=True)
        self.assertEqual([d["path"] for d in expanded["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertTrue(expanded["complete"])
        # Filtering by the referenced document's category does not pull policy.md.
        other = self.room.export_manifest("R1", category="operations",
                                          include_references=True)
        self.assertEqual([d["path"] for d in other["documents"]], ["meeting.txt"])

    def test_set_category_after_file_deleted_and_export_missing(self):
        self.room.add("policy.md", ["legal"])
        (self.root / "policy.md").unlink()
        # The category is index-only: it updates even with the file gone.
        record = self.room.set_category("policy.md", "legal")
        self.assertEqual(record["category"], "legal")
        manifest = self.room.export_manifest("R1", category="legal")
        self.assertEqual([d["path"] for d in manifest["documents"]], ["policy.md"])
        self.assertEqual(manifest["documents"][0]["status"], "missing")
        self.assertFalse(manifest["complete"])

    def test_cli_category_and_filtered_search_export(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root),
                  "--index", str(self.index)]
        self.room.add("policy.md", ["legal"])
        self._add_meeting()
        self.room.set_references("policy.md", ["meeting.txt"])
        result = subprocess.run(prefix + ["category", "policy.md", "--value", " legal "],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["category"], "legal")
        # Uncategorized records are selected by an explicit empty value.
        result = subprocess.run(prefix + ["search", "--category", ""],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["path"] for r in json.loads(result.stdout)], ["meeting.txt"])
        # Export filters the starting documents; references still expand.
        result = subprocess.run(prefix + ["export", "--release", "R1",
                                          "--category", "legal", "--with-references"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        # Unknown path is a JSON error with exit 2.
        result = subprocess.run(prefix + ["category", "ghost.md", "--value", "x"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "policy.md").write_text("Retention policy\n", encoding="utf-8")
        self.index = self.root / "index.json"
        self.room = DocumentRoom(self.root, self.index)

    def record(self, path, **overrides):
        record = {"path": path, "name": path.rsplit("/", 1)[-1], "bytes": 10,
                  "sha256": "hash-" + path, "tags": ["legal"]}
        record.update(overrides)
        return record

    def test_import_registers_verbatim_without_files(self):
        batch = {"policy.md": self.record("policy.md", tags=[" Legal ", "POLICY"],
                                          references=["meeting.txt"],
                                          extra={"nested": [1, 2]}),
                 "meeting.txt": self.record("meeting.txt", tags=[],
                                            category=" Ops ", archived=True)}
        result = self.room.import_records(batch)
        self.assertEqual(result, {"added": ["meeting.txt", "policy.md"], "unchanged": []})
        # Records are stored as given: no path, tag or category normalization,
        # no optional fields added, extra fields preserved.
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored, batch)
        # No document file exists, yet search returns the full records.
        self.assertEqual(self.room.search(), [batch["meeting.txt"], batch["policy.md"]])
        # Tags were stored verbatim, so the normalized query tag matches nothing.
        self.assertEqual(self.room.search(["legal"]), [])
        self.assertEqual(self.room.search(text="MEETING"), [batch["meeting.txt"]])
        # Export expands the forward reference and reports both files missing.
        manifest = self.room.export_manifest("R1", text="policy",
                                             include_references=True, include_origins=True)
        self.assertEqual([d["path"] for d in manifest["documents"]],
                         ["meeting.txt", "policy.md"])
        self.assertEqual({d["path"]: d["status"] for d in manifest["documents"]},
                         {"meeting.txt": "missing", "policy.md": "changed"})
        self.assertFalse(manifest["complete"])
        origins = {d["path"]: d["origins"] for d in manifest["documents"]}
        self.assertEqual(origins["meeting.txt"],
                         [{"path": "policy.md", "distance": 1}])
        self.assertEqual(origins["policy.md"], [{"path": "policy.md", "distance": 0}])

    def test_import_merges_with_existing_index(self):
        self.room.add("policy.md", ["legal"])
        existing = json.loads(self.index.read_text(encoding="utf-8"))
        batch = {"meeting.txt": self.record("meeting.txt", references=["policy.md"])}
        result = self.room.import_records(batch)
        self.assertEqual(result, {"added": ["meeting.txt"], "unchanged": []})
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["policy.md"], existing["policy.md"])
        self.assertEqual(stored["meeting.txt"], batch["meeting.txt"])

    def test_identical_reimport_is_unchanged_and_not_rewritten(self):
        batch = {"b.md": self.record("b.md"), "a.md": self.record("a.md")}
        self.assertEqual(self.room.import_records(batch)["added"], ["a.md", "b.md"])
        before = self.index.read_text(encoding="utf-8")
        # Object key order does not participate in the comparison.
        shuffled = {path: dict(reversed(list(record.items())))
                    for path, record in batch.items()}
        result = self.room.import_records(shuffled)
        self.assertEqual(result, {"added": [], "unchanged": ["a.md", "b.md"]})
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_empty_batch_and_missing_index_create_nothing(self):
        self.assertEqual(self.room.import_records({}), {"added": [], "unchanged": []})
        self.assertFalse(self.index.exists())
        self.assertFalse((self.root / "nested").exists())
        nested = DocumentRoom(self.root, self.root / "nested" / "index.json")
        self.assertEqual(nested.import_records({}), {"added": [], "unchanged": []})
        self.assertFalse((self.root / "nested").exists())

    def test_conflict_raises_and_leaves_index_untouched(self):
        self.room.import_records({"policy.md": self.record("policy.md")})
        before = self.index.read_text(encoding="utf-8")
        conflicting = {"policy.md": self.record("policy.md", bytes=11),
                       "new.md": self.record("new.md")}
        with self.assertRaises(ValueError):
            self.room.import_records(conflicting)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_duplicate_judgement_field_set_and_array_order(self):
        self.room.import_records({"a.md": self.record("a.md", tags=["x", "y"]),
                                  "b.md": self.record("b.md")})
        before = self.index.read_text(encoding="utf-8")
        # Array order participates in the comparison.
        with self.assertRaises(ValueError):
            self.room.import_records({"a.md": self.record("a.md", tags=["y", "x"])})
        # A missing optional field differs from its explicit default.
        with self.assertRaises(ValueError):
            self.room.import_records({"b.md": self.record("b.md", archived=False)})
        with self.assertRaises(ValueError):
            self.room.import_records({"b.md": self.record("b.md", references=[])})
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)
        # An explicitly stored default matches only the same explicit default.
        self.room.import_records({"c.md": self.record("c.md", archived=False)})
        result = self.room.import_records({"c.md": self.record("c.md", archived=False)})
        self.assertEqual(result["unchanged"], ["c.md"])

    def test_invalid_batch_raises_and_creates_nothing(self):
        good = self.record("ok.md")
        bad_batches = [
            [good],  # not an object
            {"ok.md": ["not", "a", "record"]},
            {"ok.md": self.record("other.md")},  # key/path mismatch
            {"ok.md": self.record("ok.md", name=1)},
            {"ok.md": self.record("ok.md", bytes=True)},
            {"ok.md": self.record("ok.md", bytes="10")},
            {"ok.md": self.record("ok.md", sha256=5)},
            {"ok.md": self.record("ok.md", tags="legal")},
            {"ok.md": self.record("ok.md", tags=["legal", 2])},
            {"ok.md": self.record("ok.md", archived="yes")},
            {"ok.md": self.record("ok.md", references="meeting.txt")},
            {"ok.md": self.record("ok.md", references=[1])},
            {"ok.md": self.record("ok.md", category=7)},
        ]
        for batch in bad_batches:
            with self.assertRaises(ValueError):
                self.room.import_records(batch)
        with self.assertRaises(ValueError):
            self.room.import_records(["not", "a", "dict"])
        self.assertFalse(self.index.exists())

    def test_unregistered_reference_fails_whole_batch(self):
        batch = {"a.md": self.record("a.md", references=["ghost.md"]),
                 "b.md": self.record("b.md")}
        with self.assertRaises(ValueError):
            self.room.import_records(batch)
        self.assertFalse(self.index.exists())

    def test_forward_self_and_cyclic_references_allowed(self):
        batch = {"policy.md": self.record("policy.md", references=["meeting.txt"]),
                 "meeting.txt": self.record("meeting.txt",
                                            references=["policy.md", "meeting.txt"])}
        result = self.room.import_records(batch)
        self.assertEqual(result["added"], ["meeting.txt", "policy.md"])
        impact = self.room.reference_impact("meeting.txt", transitive=True)
        self.assertEqual([d["path"] for d in impact["documents"]], ["policy.md"])

    def test_reference_may_point_at_existing_record(self):
        self.room.add("policy.md")
        result = self.room.import_records(
            {"meeting.txt": self.record("meeting.txt", references=["policy.md"])})
        self.assertEqual(result["added"], ["meeting.txt"])

    def test_bad_existing_index_record_fails_import(self):
        self.room.add("policy.md")
        records = json.loads(self.index.read_text(encoding="utf-8"))
        records["junk.md"] = {"path": "junk.md", "name": "junk.md", "bytes": True,
                              "sha256": "x", "tags": []}
        self.index.write_text(json.dumps(records), encoding="utf-8")
        before = self.index.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.import_records({"new.md": self.record("new.md")})
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)
        # A stored reference the batch would satisfy is resolved against the union.
        records["junk.md"] = self.record("junk.md", references=["new.md"])
        self.index.write_text(json.dumps(records), encoding="utf-8")
        result = self.room.import_records({"new.md": self.record("new.md")})
        self.assertEqual(result["added"], ["new.md"])

    def test_corrupt_index_and_read_failure(self):
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.import_records({"a.md": self.record("a.md")})
        self.index.write_text(json.dumps([1, 2]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.import_records({"a.md": self.record("a.md")})
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.import_records({"a.md": self.record("a.md")})
        finally:
            os.chmod(self.index, 0o644)

    def test_cli_import_exit_codes_and_payload(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"),
                  "--root", str(self.root), "--index", str(self.index)]
        batch_path = self.root / "batch.json"
        batch = {"meeting.txt": self.record("meeting.txt"),
                 "policy.md": self.record("policy.md", references=["meeting.txt"])}
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        result = subprocess.run(prefix + ["import", "--from", str(batch_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"added": ["meeting.txt", "policy.md"], "unchanged": []})
        self.assertEqual(json.loads(batch_path.read_text(encoding="utf-8")), batch)
        # A conflicting second import is a JSON error with exit 2.
        batch["policy.md"]["bytes"] = 999
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        result = subprocess.run(prefix + ["import", "--from", str(batch_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        # Invalid JSON, invalid UTF-8 and a missing source file all exit 2.
        batch_path.write_text("{not json", encoding="utf-8")
        result = subprocess.run(prefix + ["import", "--from", str(batch_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        batch_path.write_bytes(b"\xff\xfe{}")
        result = subprocess.run(prefix + ["import", "--from", str(batch_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        result = subprocess.run(
            prefix + ["import", "--from", str(self.root / "missing.json")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        # The failed imports never altered the index from the first success.
        stored = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(stored["policy.md"]["bytes"], 10)


class ManifestComparisonTests(unittest.TestCase):
    def doc(self, path, **overrides):
        document = {"path": path, "name": path.rsplit("/", 1)[-1], "bytes": 10,
                    "sha256": "hash-" + path, "tags": ["a"], "status": "ready"}
        document.update(overrides)
        return document

    def manifest(self, documents, release="R1", complete=True):
        return {"release": release, "complete": complete, "documents": documents}

    def test_categories_and_sorting(self):
        before = self.manifest([
            self.doc("b/c.md"), self.doc("removed.md", status="missing"),
            self.doc("same.md"), self.doc("z.md", name="old")])
        after = self.manifest([
            self.doc("a/new.md"), self.doc("b/c.md", bytes=99),
            self.doc("same.md"), self.doc("z.md", name="new")],
            release="R2", complete=False)
        result = DocumentRoom.compare_manifests(before, after)
        self.assertEqual(result["before"], {"release": "R1", "complete": True})
        self.assertEqual(result["after"], {"release": "R2", "complete": False})
        self.assertEqual([d["path"] for d in result["added"]], ["a/new.md"])
        self.assertEqual([d["path"] for d in result["removed"]], ["removed.md"])
        self.assertEqual([c["path"] for c in result["changed"]], ["b/c.md", "z.md"])
        self.assertEqual(result["unchanged"], ["same.md"])
        # Added/removed carry the complete document from the right side.
        self.assertEqual(result["added"][0], self.doc("a/new.md"))
        self.assertEqual(result["removed"][0]["status"], "missing")

    def test_changed_keeps_both_documents_and_sorted_fields(self):
        before = self.manifest([self.doc("d.md", bytes=1, name="n",
                                         sha256="s", tags=["x"], status="ready")])
        after = self.manifest([self.doc("d.md", bytes=2, name="m",
                                        sha256="t", tags=["y"], status="changed")])
        (item,) = DocumentRoom.compare_manifests(before, after)["changed"]
        self.assertEqual(item["path"], "d.md")
        self.assertEqual(item["before"], before["documents"][0])
        self.assertEqual(item["after"], after["documents"][0])
        self.assertEqual(item["fields"], ["bytes", "name", "sha256", "status", "tags"])

    def test_status_only_change_is_changed(self):
        before = self.manifest([self.doc("d.md", status="ready")])
        after = self.manifest([self.doc("d.md", status="unreadable")], complete=False)
        result = DocumentRoom.compare_manifests(before, after)
        self.assertEqual([c["fields"] for c in result["changed"]], [["status"]])
        self.assertEqual(result["unchanged"], [])

    def test_tags_compared_as_sets_but_case_and_whitespace_significant(self):
        cases = ((["a", "b", "a"], ["b", "a"], []),
                 (["a"], ["A"], ["tags"]),
                 (["a"], [" a "], ["tags"]),
                 (["a", "b"], ["a"], ["tags"]))
        for old_tags, new_tags, fields in cases:
            result = DocumentRoom.compare_manifests(
                self.manifest([self.doc("d.md", tags=old_tags)]),
                self.manifest([self.doc("d.md", tags=new_tags)]))
            self.assertEqual(
                [c["fields"] for c in result["changed"]] + result["unchanged"],
                [fields] if fields else ["d.md"], msg=f"{old_tags} vs {new_tags}")

    def test_path_pairing_is_case_sensitive_and_exact(self):
        before = self.manifest([self.doc("Doc.md"), self.doc("a/../x.md")])
        after = self.manifest([self.doc("doc.md"), self.doc("a/../x.md"),
                               self.doc("a/x.md")])
        result = DocumentRoom.compare_manifests(before, after)
        self.assertEqual([d["path"] for d in result["added"]], ["a/x.md", "doc.md"])
        self.assertEqual([d["path"] for d in result["removed"]], ["Doc.md"])
        self.assertEqual(result["unchanged"], ["a/../x.md"])

    def test_release_and_complete_do_not_affect_classification(self):
        before = self.manifest([self.doc("d.md")], release="R1", complete=False)
        after = self.manifest([self.doc("d.md")], release="Different", complete=True)
        result = DocumentRoom.compare_manifests(before, after)
        self.assertEqual(result["unchanged"], ["d.md"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["before"], {"release": "R1", "complete": False})
        self.assertEqual(result["after"], {"release": "Different", "complete": True})

    def test_extra_fields_preserved_but_not_compared(self):
        before = self.manifest([self.doc("d.md", references=["x"], note="old")])
        after = self.manifest([self.doc("d.md", references=["y"], note="new")])
        result = DocumentRoom.compare_manifests(before, after)
        self.assertEqual(result["unchanged"], ["d.md"])
        # Extra top-level fields are tolerated too.
        before["generated_by"] = "export"
        self.assertEqual(DocumentRoom.compare_manifests(before, after)["unchanged"], ["d.md"])

    def test_both_empty(self):
        result = DocumentRoom.compare_manifests(self.manifest([]), self.manifest([]))
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["unchanged"], [])
        self.assertEqual(result["before"], {"release": "R1", "complete": True})

    def test_inputs_are_not_modified(self):
        before = self.manifest([self.doc("d.md", tags=["b", "a", "a"])])
        after = self.manifest([self.doc("d.md", tags=["a", "b"])])
        snapshot = json.dumps([before, after], sort_keys=True)
        DocumentRoom.compare_manifests(before, after)
        self.assertEqual(json.dumps([before, after], sort_keys=True), snapshot)

    def test_invalid_manifests_raise_value_error(self):
        valid = self.manifest([self.doc("d.md")])

        def expect_value_error(before, after):
            with self.assertRaises(ValueError):
                DocumentRoom.compare_manifests(before, after)

        expect_value_error([], valid)
        expect_value_error({}, valid)
        bad = json.loads(json.dumps(valid))
        bad["release"] = "   "
        expect_value_error(bad, valid)
        bad = json.loads(json.dumps(valid))
        bad["release"] = 5
        expect_value_error(bad, valid)
        bad = json.loads(json.dumps(valid))
        bad["complete"] = "yes"
        expect_value_error(bad, valid)
        bad = json.loads(json.dumps(valid))
        bad["documents"] = {}
        expect_value_error(bad, valid)
        for field, bad_value in (("path", 1), ("name", 1), ("sha256", 1),
                                 ("bytes", 1.5), ("bytes", True),
                                 ("tags", "a"), ("tags", ["a", 1]),
                                 ("status", "draft")):
            bad = json.loads(json.dumps(valid))
            bad["documents"][0][field] = bad_value
            expect_value_error(bad, valid)
            expect_value_error(valid, bad)
        for missing in ("release", "complete", "documents"):
            bad = json.loads(json.dumps(valid))
            del bad[missing]
            expect_value_error(bad, valid)
        for missing in ("path", "name", "sha256", "bytes", "tags", "status"):
            bad = json.loads(json.dumps(valid))
            del bad["documents"][0][missing]
            expect_value_error(bad, valid)
        bad = json.loads(json.dumps(valid))
        bad["documents"].append(self.doc("d.md"))
        expect_value_error(bad, valid)
        bad = json.loads(json.dumps(valid))
        bad["documents"] = ["not-an-object"]
        expect_value_error(bad, valid)

    def test_file_based_compare_matches_dict_api(self):
        before = self.manifest([self.doc("old.md"), self.doc("same.md"),
                                self.doc("t.md", tags=["a", "b"])])
        after = self.manifest([self.doc("new.md"), self.doc("same.md"),
                               self.doc("t.md", tags=["b", "a", "b"])],
                              release="R2", complete=False)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            before_path = Path(temp) / "before.json"
            after_path = Path(temp) / "after.json"
            before_path.write_text(json.dumps(before), encoding="utf-8")
            after_path.write_text(json.dumps(after), encoding="utf-8")
            from_files = DocumentRoom.compare_manifest_files(before_path, after_path)
        self.assertEqual(from_files, DocumentRoom.compare_manifests(before, after))
        self.assertEqual([d["path"] for d in from_files["removed"]], ["old.md"])
        self.assertEqual([d["path"] for d in from_files["added"]], ["new.md"])
        self.assertEqual(from_files["unchanged"], ["same.md", "t.md"])

    def test_cli_compare_exit_codes_and_payload(self):
        prefix = [sys.executable, str(ROOT / "document_room.py")]
        before = self.manifest([self.doc("old.md"), self.doc("d.md", status="ready")])
        after = self.manifest([self.doc("new.md"), self.doc("d.md", status="missing")],
                              complete=False)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            before_path = Path(temp) / "before.json"
            after_path = Path(temp) / "after.json"
            before_path.write_text(json.dumps(before), encoding="utf-8")
            after_path.write_text(json.dumps(after), encoding="utf-8")
            result = subprocess.run(
                prefix + ["compare", "--before", str(before_path),
                          "--after", str(after_path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual([d["path"] for d in payload["added"]], ["new.md"])
            self.assertEqual([d["path"] for d in payload["removed"]], ["old.md"])
            self.assertEqual(payload["changed"][0]["fields"], ["status"])
            # Malformed JSON: error payload with exit 2.
            bad_json = Path(temp) / "bad.json"
            bad_json.write_text("{not json", encoding="utf-8")
            result = subprocess.run(
                prefix + ["compare", "--before", str(bad_json),
                          "--after", str(after_path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))
            # Invalid UTF-8: error payload with exit 2.
            bad_utf8 = Path(temp) / "utf8.json"
            bad_utf8.write_bytes(b'\xff\xfe{"release": "R"}')
            result = subprocess.run(
                prefix + ["compare", "--before", str(bad_utf8),
                          "--after", str(after_path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))
            # Missing file: OSError surfaces as error payload with exit 2.
            result = subprocess.run(
                prefix + ["compare", "--before", str(Path(temp) / "missing.json"),
                          "--after", str(after_path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))
            # Input files are untouched and no files are created.
            self.assertEqual(json.loads(before_path.read_text(encoding="utf-8")), before)
            self.assertEqual(set(os.listdir(temp)),
                             {"before.json", "after.json", "bad.json", "utf8.json"})


class DumpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "policy.md").write_text("Retention policy\n", encoding="utf-8")
        (self.root / "meeting.txt").write_text("Notes\n", encoding="utf-8")
        (self.root / "notes.md").write_text("More\n", encoding="utf-8")
        self.index = self.root / "index.json"
        self.room = DocumentRoom(self.root, self.index)
        self.policy = self.room.add("policy.md", [" Legal ", "POLICY"])
        self.meeting = self.room.add("meeting.txt", ["operations"])
        self.notes = self.room.add("notes.md", ["operations"])
        self.room.set_references("policy.md", ["meeting.txt"])
        self.room.set_references("meeting.txt", ["notes.md", "meeting.txt"])

    def test_filter_start_then_close_over_forward_references(self):
        snapshot = self.room.export_records(["legal"], "POLICY")
        self.assertEqual(list(snapshot), ["meeting.txt", "notes.md", "policy.md"])
        # Only policy.md matches the filter; both referenced records come along.
        self.assertEqual(snapshot, {"meeting.txt": {**self.meeting,
                                                    "references": ["meeting.txt", "notes.md"]},
                                    "notes.md": self.notes,
                                    "policy.md": {**self.policy,
                                                 "references": ["meeting.txt"]}})

    def test_referrers_are_not_pulled_in(self):
        # meeting.txt does not reference policy.md, so filtering by operations
        # never includes the policy that points back at meeting.txt.
        snapshot = self.room.export_records(["operations"])
        self.assertEqual(list(snapshot), ["meeting.txt", "notes.md"])
        self.assertNotIn("policy.md", snapshot)

    def test_shared_references_self_references_and_cycles(self):
        (self.root / "shared.md").write_text("x\n", encoding="utf-8")
        shared = self.room.add("shared.md")
        self.room.set_references("meeting.txt", ["notes.md", "shared.md", "meeting.txt"])
        self.room.set_references("notes.md", ["policy.md"])  # cycle back
        snapshot = self.room.export_records(text="policy")
        self.assertEqual(list(snapshot),
                         ["meeting.txt", "notes.md", "policy.md", "shared.md"])
        self.assertEqual(snapshot["shared.md"], shared)

    def test_archive_and_category_filters_only_choose_starts(self):
        self.room.set_archived("meeting.txt", True)
        self.room.set_category("policy.md", "legal")
        self.room.set_category("notes.md", "ops")
        # Archived dependency is still pulled in for an active start.
        snapshot = self.room.export_records(archive_state="active")
        self.assertEqual(list(snapshot),
                         ["meeting.txt", "notes.md", "policy.md"])
        self.assertTrue(snapshot["meeting.txt"]["archived"])
        # Category filter: only policy.md starts; deps cross categories.
        snapshot = self.room.export_records(category="legal")
        self.assertEqual(list(snapshot), ["meeting.txt", "notes.md", "policy.md"])
        # Filters combine conjunctively.
        self.assertEqual(list(self.room.export_records(["legal"], archive_state="archived")),
                         [])

    def test_records_exported_verbatim_without_live_files(self):
        before = self.index.read_text(encoding="utf-8")
        stored = json.loads(before)
        stored["policy.md"]["extra"] = {"nested": [2, 1, 1], "ws": "  keep "}
        self.index.write_text(json.dumps(stored, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        (self.root / "meeting.txt").unlink()
        snapshot = self.room.export_records(text="policy")
        # Extra fields, array order, duplicates, case and whitespace survive;
        # missing optional fields are not filled in.
        self.assertEqual(snapshot["policy.md"]["extra"],
                         {"nested": [2, 1, 1], "ws": "  keep "})
        self.assertEqual(snapshot["meeting.txt"]["references"],
                         ["meeting.txt", "notes.md"])
        self.assertNotIn("references", snapshot["notes.md"])
        self.assertNotIn("archived", snapshot["notes.md"])
        self.assertNotIn("category", snapshot["notes.md"])
        # The index is untouched and no snapshot file is written.
        self.assertEqual(sorted(os.listdir(self.root)), ["index.json", "notes.md",
                                                         "policy.md"])

    def test_snapshot_roundtrips_into_empty_index(self):
        snapshot = self.room.export_records(["legal"])
        target = DocumentRoom(self.root, self.root / "other" / "index.json")
        result = target.import_records(snapshot)
        self.assertEqual(result, {"added": ["meeting.txt", "notes.md", "policy.md"],
                                  "unchanged": []})
        self.assertEqual(json.loads((self.root / "other" / "index.json").read_text(
            encoding="utf-8")), snapshot)

    def test_default_selects_every_record_sorted_case_sensitively(self):
        (self.root / "B.md").write_text("b\n", encoding="utf-8")
        self.room.add("B.md")
        snapshot = self.room.export_records()
        self.assertEqual(list(snapshot),
                         ["B.md", "meeting.txt", "notes.md", "policy.md"])

    def test_missing_index_or_no_starts_returns_empty_object(self):
        fresh = DocumentRoom(self.root, self.root / "fresh.json")
        self.assertEqual(fresh.export_records(), {})
        self.assertFalse((self.root / "fresh.json").exists())
        self.assertEqual(self.room.export_records(["nope"]), {})
        self.assertEqual(self.room.export_records(text="zzz"), {})

    def test_filter_arguments_validated(self):
        for bad in (None, 7, "legal", ["legal", 1], [0]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.export_records(bad)
        for bad in (None, 7, ["x"], b"x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.export_records(text=bad)
        for bad in (None, 7, "active ", ["active"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.export_records(archive_state=bad)
        for bad in (7, True, ["legal"], b"legal"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.room.export_records(category=bad)

    def test_whole_index_validated_even_when_bad_record_unselected(self):
        valid = self.index.read_text(encoding="utf-8")
        # A dangling reference on an unrelated record fails an empty selection.
        stored = json.loads(valid)
        stored["notes.md"]["references"] = ["ghost.md"]
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_records(text="zzz")
        # Malformed record structure and bad JSON fail too.
        stored = json.loads(valid)
        stored["notes.md"]["tags"] = "ops"
        self.index.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_records()
        self.index.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_records()
        self.index.write_text(json.dumps([1, 2]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.room.export_records()
        self.index.write_bytes(b"\xff\xfe{}")
        with self.assertRaises(ValueError):
            self.room.export_records()
        self.index.write_text(valid, encoding="utf-8")
        os.chmod(self.index, 0)
        try:
            with self.assertRaises(OSError):
                self.room.export_records()
        finally:
            os.chmod(self.index, 0o644)

    def test_cli_dump_exit_codes_and_roundtrip(self):
        prefix = [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root),
                  "--index", str(self.index)]
        result = subprocess.run(prefix + ["dump", "--tag", "LEGAL", "--text", "POLICY",
                                          "--archive-state", "active"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual(list(snapshot), ["meeting.txt", "notes.md", "policy.md"])
        # Import the printed snapshot into an empty index via the CLI.
        other = self.root / "other.json"
        snap_path = self.root / "snapshot.json"
        snap_path.write_text(result.stdout, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root),
             "--index", str(other), "import", "--from", str(snap_path)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(other.read_text(encoding="utf-8")), snapshot)
        # Missing index and no starts print {} with exit 0 and create nothing.
        result = subprocess.run(
            [sys.executable, str(ROOT / "document_room.py"), "--root", str(self.root),
             "--index", str(self.root / "none.json"), "dump"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{}")
        self.assertFalse((self.root / "none.json").exists())
        # Corrupt index and an illegal archive state print an error with exit 2.
        self.index.write_text("{not json", encoding="utf-8")
        result = subprocess.run(prefix + ["dump"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        result = subprocess.run(prefix + ["dump", "--archive-state", "bogus"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))
        # No partial snapshot is printed on failure.
        self.assertNotIn("policy.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
