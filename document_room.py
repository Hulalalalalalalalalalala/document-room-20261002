"""A local document metadata index with conjunctive tag search and release manifests."""
import argparse
import hashlib
import json
from pathlib import Path


class DocumentRoom:
    STATUSES = {"ready", "changed", "missing", "unsafe", "unreadable"}
    COMPARED_FIELDS = ("bytes", "name", "sha256", "status", "tags")
    ARCHIVE_STATES = ("all", "active", "archived")

    def __init__(self, root, index):
        self.root = Path(root).resolve()
        self.index = Path(index)

    def _load(self):
        return json.loads(self.index.read_text(encoding="utf-8")) if self.index.exists() else {}

    def _load_validated(self):
        records = self._load()
        if not isinstance(records, dict):
            raise ValueError("index must be a JSON object of records")
        for key, record in records.items():
            if (not isinstance(key, str) or not isinstance(record, dict)
                    or record.get("path") != key):
                raise ValueError("index records must be objects keyed by their path")
            if (not isinstance(record.get("name"), str)
                    or not isinstance(record.get("bytes"), int)
                    or isinstance(record.get("bytes"), bool)
                    or not isinstance(record.get("sha256"), str)
                    or not isinstance(record.get("tags"), list)
                    or not all(isinstance(tag, str) for tag in record["tags"])):
                raise ValueError("index record has invalid field types")
            if "archived" in record and not isinstance(record["archived"], bool):
                raise ValueError("index record archived must be a boolean")
        return records

    @staticmethod
    def _check_archive_state(archive_state):
        if not isinstance(archive_state, str) or archive_state not in DocumentRoom.ARCHIVE_STATES:
            raise ValueError("archive_state must be one of all, active, archived")

    @staticmethod
    def _matches_archive_state(record, archive_state):
        if archive_state == "all":
            return True
        is_archived = record.get("archived", False)
        return is_archived if archive_state == "archived" else not is_archived

    @staticmethod
    def _check_category_filter(category):
        if category is not None and not isinstance(category, str):
            raise ValueError("category filter must be a string or None")

    @staticmethod
    def _matches_category(record, category):
        if category is None:
            return True
        return record.get("category", "").strip() == category.strip()

    def _load_validated_with_references(self):
        records = self._load_validated()
        for record in records.values():
            references = record.get("references", [])
            if (not isinstance(references, list)
                    or not all(isinstance(target, str) for target in references)):
                raise ValueError("index record references must be a list of strings")
            for target in references:
                if target not in records:
                    raise ValueError("index record references an unregistered path")
        return records

    def _load_validated_with_categories(self):
        records = self._load_validated_with_references()
        for record in records.values():
            if "category" in record and not isinstance(record["category"], str):
                raise ValueError("index record category must be a string")
        return records

    def add(self, relative_path, tags=()):
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("document must be a file inside the document root")
        clean_tags = sorted({tag.strip().lower() for tag in tags if tag.strip()})
        key = path.relative_to(self.root).as_posix()
        content = path.read_bytes()
        record = {"path": key, "name": path.name, "bytes": len(content),
                  "sha256": hashlib.sha256(content).hexdigest(), "tags": clean_tags}
        records = self._load()
        existing = records.get(key)
        if isinstance(existing, dict) and "references" in existing:
            record["references"] = existing["references"]
        if isinstance(existing, dict) and "archived" in existing:
            record["archived"] = existing["archived"]
        if isinstance(existing, dict) and "category" in existing:
            record["category"] = existing["category"]
        records[key] = record
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    @staticmethod
    def _validate_records(records, check_targets=True):
        # Full registered-format validation of any record mapping: required
        # field types, optional references/archived/category types, and (unless
        # suppressed) every reference target present among the records.
        if not isinstance(records, dict):
            raise ValueError("index must be a JSON object of records")
        for key, record in records.items():
            if (not isinstance(key, str) or not isinstance(record, dict)
                    or record.get("path") != key):
                raise ValueError("index records must be objects keyed by their path")
            if (not isinstance(record.get("name"), str)
                    or not isinstance(record.get("bytes"), int)
                    or isinstance(record.get("bytes"), bool)
                    or not isinstance(record.get("sha256"), str)
                    or not isinstance(record.get("tags"), list)
                    or not all(isinstance(tag, str) for tag in record["tags"])):
                raise ValueError("index record has invalid field types")
            if "archived" in record and not isinstance(record["archived"], bool):
                raise ValueError("index record archived must be a boolean")
            references = record.get("references", [])
            if (not isinstance(references, list)
                    or not all(isinstance(target, str) for target in references)):
                raise ValueError("index record references must be a list of strings")
            if "category" in record and not isinstance(record["category"], str):
                raise ValueError("index record category must be a string")
        if check_targets:
            for record in records.values():
                for target in record.get("references", []):
                    if target not in records:
                        raise ValueError("index record references an unregistered path")
        return records

    def import_records(self, records):
        if not isinstance(records, dict):
            raise ValueError("import payload must be a JSON object of records")
        # Validate existing field types without requiring reference targets yet:
        # a dangling target there may be supplied by this batch.
        existing = self._validate_records(self._load(), check_targets=False)
        added = []
        unchanged = []
        for key, incoming in records.items():
            if not isinstance(key, str):
                raise ValueError("import records must be keyed by string paths")
            if key in existing:
                # Plain dict/list equality: the field set must match exactly
                # (a missing optional field differs from a stored default),
                # array order matters and object key order does not.
                if existing[key] != incoming:
                    raise ValueError(
                        f"imported record conflicts with an existing path: {key}")
                unchanged.append(key)
            else:
                added.append(key)
        merged = {**existing, **records}
        # Full validation of input plus merged index; reference targets only
        # need to exist in the union, so forward/self/cyclic references within
        # the batch (or completing a dangling existing one) are accepted.
        self._validate_records(merged)
        if added:  # an empty or fully identical batch never touches the index
            self.index.parent.mkdir(parents=True, exist_ok=True)
            self.index.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        return {"added": sorted(added), "unchanged": sorted(unchanged)}

    def set_references(self, relative_path, targets):
        if not isinstance(relative_path, str):
            raise ValueError("source path must be a string")
        if (not isinstance(targets, list)
                or not all(isinstance(target, str) for target in targets)):
            raise ValueError("targets must be a list of strings")
        records = self._load_validated_with_references()
        if relative_path not in records:
            raise ValueError("source document is not registered")
        for target in targets:
            if target not in records:
                raise ValueError("reference target is not registered")
        record = records[relative_path]
        record["references"] = sorted(set(targets))
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    def set_archived(self, relative_path, archived):
        if not isinstance(relative_path, str):
            raise ValueError("document path must be a string")
        if not isinstance(archived, bool):
            raise ValueError("archived must be a boolean")
        records = self._load_validated_with_references()
        if relative_path not in records:
            raise ValueError("document is not registered")
        record = records[relative_path]
        record["archived"] = archived
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    def set_category(self, relative_path, category):
        if not isinstance(relative_path, str):
            raise ValueError("document path must be a string")
        if not isinstance(category, str):
            raise ValueError("category must be a string")
        records = self._load_validated_with_categories()
        if relative_path not in records:
            raise ValueError("document is not registered")
        record = records[relative_path]
        record["category"] = category.strip()
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    def reference_impact(self, target, transitive=False):
        if not isinstance(target, str):
            raise ValueError("target path must be a string")
        if not isinstance(transitive, bool):
            raise ValueError("transitive must be a boolean")
        records = self._load_validated_with_references()
        if target not in records:
            raise ValueError("target document is not registered")
        referrers = {}
        for source, record in records.items():
            for referenced in record.get("references", []):
                referrers.setdefault(referenced, set()).add(source)
        distances = {}
        frontier = [target]
        depth = 0
        while frontier and (transitive or depth == 0):
            nxt = []
            for node in frontier:
                for source in referrers.get(node, ()):  # reverse reference edges
                    if source != target and source not in distances:
                        distances[source] = depth + 1
                        nxt.append(source)
            frontier = nxt
            depth += 1
        documents = [{**records[key], "distance": distances[key]}
                     for key in sorted(distances, key=lambda key: (distances[key], key))]
        return {"path": target, "documents": documents}

    def search(self, tags=(), text="", archive_state="all", category=None):
        self._check_archive_state(archive_state)
        self._check_category_filter(category)
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}
        if archive_state == "all" and category is None:
            records = self._load()
        else:  # a non-trivial filter validates the whole index, references included
            records = self._load_validated_with_categories()
        return [record for _, record in sorted(records.items())
                if wanted.issubset(record["tags"])
                and text.casefold() in record["path"].casefold()
                and self._matches_archive_state(record, archive_state)
                and self._matches_category(record, category)]

    def find_duplicates(self, tags=(), text=""):
        if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("tags must be a list or tuple of strings")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        records = self._load_validated_with_references()
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}

        def matches(record):
            return (wanted.issubset(record["tags"])
                    and text.casefold() in record["path"].casefold())

        buckets = {}
        for record in records.values():
            buckets.setdefault((record["bytes"], record["sha256"]), []).append(record)
        groups = []
        for (size, digest), members in buckets.items():
            if len(members) < 2:  # duplicates need at least two distinct paths
                continue
            paths = sorted(record["path"] for record in members)
            matched = [path for path in paths if matches(records[path])]
            if not matched:  # a filter must hit at least one member to return the group
                continue
            groups.append({"bytes": size, "sha256": digest, "matched": matched,
                           "documents": [records[path] for path in paths]})
        groups.sort(key=lambda group: group["documents"][0]["path"])
        return {"groups": groups}

    def _file_status(self, record):
        try:
            path = (self.root / record["path"]).resolve()
            if not path.is_relative_to(self.root):
                return "unsafe"
            if not path.is_file():
                return "missing"
            content = path.read_bytes()
            return ("ready" if len(content) == record["bytes"]
                    and hashlib.sha256(content).hexdigest() == record["sha256"]
                    else "changed")
        except (OSError, RuntimeError, ValueError):
            return "unreadable"

    def export_manifest(self, release, tags=(), text="", include_references=False,
                        archive_state="all", category=None, include_origins=False):
        if not isinstance(release, str) or not release.strip():
            raise ValueError("release must be a non-empty string")
        self._check_archive_state(archive_state)
        self._check_category_filter(category)
        if not isinstance(include_origins, bool):
            raise ValueError("include_origins must be a boolean")
        if include_origins and not include_references:
            raise ValueError("include_origins requires include_references")
        release = release.strip()
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}

        def matches(record):
            return (wanted.issubset(record["tags"])
                    and text.casefold() in record["path"].casefold()
                    and self._matches_archive_state(record, archive_state)
                    and self._matches_category(record, category))

        if include_references or archive_state != "all" or category is not None:
            records = self._load_validated_with_categories()
        else:
            records = self._load_validated()
        if include_references:
            selected = {}
            starts = [key for key, record in records.items() if matches(record)]
            stack = list(starts)
            while stack:
                key = stack.pop()
                if key in selected:
                    continue
                selected[key] = records[key]
                # Referenced documents are pulled in regardless of archive state
                # or category.
                stack.extend(records[key].get("references", []))
            ordered = [selected[key] for key in sorted(selected)]
            origins = {}
            if include_origins:
                # Breadth-first from each starting document along the reference
                # direction; every reachable document keeps the shortest distance
                # per starting source.
                for start in starts:
                    distances = {start: 0}
                    frontier = [start]
                    depth = 0
                    while frontier:
                        nxt = []
                        for node in frontier:
                            for target in records[node].get("references", []):
                                if target not in distances:
                                    distances[target] = depth + 1
                                    nxt.append(target)
                        frontier = nxt
                        depth += 1
                    for key, distance in distances.items():
                        per_document = origins.setdefault(key, {})
                        if start not in per_document or distance < per_document[start]:
                            per_document[start] = distance
        else:
            ordered = [record for _, record in sorted(records.items()) if matches(record)]
        documents = []
        for record in ordered:
            document = {"path": record["path"], "name": record["name"],
                        "bytes": record["bytes"], "sha256": record["sha256"],
                        "tags": record["tags"], "status": self._file_status(record)}
            if include_origins:
                document["origins"] = [
                    {"path": source, "distance": origins[record["path"]][source]}
                    for source in sorted(origins[record["path"]])]
            documents.append(document)
        return {"release": release,
                "complete": bool(documents) and all(d["status"] == "ready" for d in documents),
                "documents": documents}

    @staticmethod
    def _validated_manifest(manifest):
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        release = manifest.get("release")
        if not isinstance(release, str) or not release.strip():
            raise ValueError("manifest release must be a non-blank string")
        if not isinstance(manifest.get("complete"), bool):
            raise ValueError("manifest complete must be a boolean")
        documents = manifest.get("documents")
        if not isinstance(documents, list):
            raise ValueError("manifest documents must be an array")
        seen = set()
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("manifest document must be an object")
            path = document.get("path")
            if not isinstance(path, str):
                raise ValueError("manifest document path must be a string")
            if path in seen:
                raise ValueError("manifest contains a duplicate path")
            seen.add(path)
            if not isinstance(document.get("name"), str):
                raise ValueError("manifest document name must be a string")
            if not isinstance(document.get("sha256"), str):
                raise ValueError("manifest document sha256 must be a string")
            if (not isinstance(document.get("bytes"), int)
                    or isinstance(document.get("bytes"), bool)):
                raise ValueError("manifest document bytes must be a non-boolean integer")
            tags = document.get("tags")
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ValueError("manifest document tags must be an array of strings")
            if document.get("status") not in DocumentRoom.STATUSES:
                raise ValueError("manifest document status must be one of "
                                 + ", ".join(sorted(DocumentRoom.STATUSES)))
        return manifest

    @staticmethod
    def compare_manifests(before, after):
        before = DocumentRoom._validated_manifest(before)
        after = DocumentRoom._validated_manifest(after)
        before_docs = {d["path"]: d for d in before["documents"]}
        after_docs = {d["path"]: d for d in after["documents"]}
        before_paths = set(before_docs)
        after_paths = set(after_docs)
        changed = []
        unchanged = []
        for path in sorted(before_paths & after_paths):
            old = before_docs[path]
            new = after_docs[path]
            fields = []
            for field in DocumentRoom.COMPARED_FIELDS:
                if field == "tags":
                    differs = set(old["tags"]) != set(new["tags"])
                else:
                    differs = old[field] != new[field]
                if differs:
                    fields.append(field)
            if fields:
                changed.append({"path": path, "before": old, "after": new,
                                "fields": fields})
            else:
                unchanged.append(path)
        return {
            "before": {"release": before["release"], "complete": before["complete"]},
            "after": {"release": after["release"], "complete": after["complete"]},
            "added": [after_docs[path] for path in sorted(after_paths - before_paths)],
            "removed": [before_docs[path] for path in sorted(before_paths - after_paths)],
            "changed": changed,
            "unchanged": unchanged,
        }

    @staticmethod
    def compare_manifest_files(before_path, after_path):
        def load(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        try:
            before = load(before_path)
            after = load(after_path)
        except UnicodeDecodeError as exc:
            raise ValueError(f"manifest file is not valid UTF-8: {exc}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest file is not valid JSON: {exc}")
        return DocumentRoom.compare_manifests(before, after)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="samples")
    parser.add_argument("--index", default=".state/documents.json")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("path")
    add.add_argument("--tag", action="append", default=[])
    search = commands.add_parser("search")
    search.add_argument("--tag", action="append", default=[])
    search.add_argument("--text", default="")
    search.add_argument("--archive-state", default="all")
    search.add_argument("--category", default=None)
    archive = commands.add_parser("archive")
    archive.add_argument("path")
    archive.add_argument("--restore", action="store_true")
    category = commands.add_parser("category")
    category.add_argument("path")
    category.add_argument("--value", required=True)
    refs = commands.add_parser("refs")
    refs.add_argument("path")
    refs.add_argument("--to", action="append", default=[])
    impact = commands.add_parser("impact")
    impact.add_argument("path")
    impact.add_argument("--transitive", action="store_true")
    duplicates = commands.add_parser("duplicates")
    duplicates.add_argument("--tag", action="append", default=[])
    duplicates.add_argument("--text", default="")
    export = commands.add_parser("export")
    export.add_argument("--release", default="")
    export.add_argument("--tag", action="append", default=[])
    export.add_argument("--text", default="")
    export.add_argument("--with-references", action="store_true")
    export.add_argument("--with-origins", action="store_true")
    export.add_argument("--archive-state", default="all")
    export.add_argument("--category", default=None)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    import_cmd = commands.add_parser("import")
    import_cmd.add_argument("--from", dest="source", required=True)
    args = parser.parse_args()
    try:
        room = DocumentRoom(args.root, args.index)
        if args.command == "add":
            result = room.add(args.path, args.tag)
        elif args.command == "archive":
            result = room.set_archived(args.path, not args.restore)
        elif args.command == "category":
            result = room.set_category(args.path, args.value)
        elif args.command == "search":
            result = room.search(args.tag, args.text, args.archive_state, args.category)
        elif args.command == "refs":
            result = room.set_references(args.path, args.to)
        elif args.command == "impact":
            result = room.reference_impact(args.path, transitive=args.transitive)
        elif args.command == "duplicates":
            result = room.find_duplicates(args.tag, args.text)
        elif args.command == "compare":
            result = room.compare_manifest_files(args.before, args.after)
        elif args.command == "import":
            try:
                payload = json.loads(Path(args.source).read_text(encoding="utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError(f"import file is not valid UTF-8: {exc}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"import file is not valid JSON: {exc}")
            result = room.import_records(payload)
        else:
            result = room.export_manifest(args.release, args.tag, args.text,
                                          include_references=args.with_references,
                                          archive_state=args.archive_state,
                                          category=args.category,
                                          include_origins=args.with_origins)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
