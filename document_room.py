"""A local document metadata index with conjunctive tag search and release manifests."""
import argparse
import hashlib
import json
from pathlib import Path


DOCUMENT_STATUSES = {"ready", "changed", "missing", "unsafe", "unreadable"}
COMPARED_FIELDS = ("name", "bytes", "sha256", "tags", "status")


class DocumentRoom:
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
        return records

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
        records[key] = record
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

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

    def search(self, tags=(), text=""):
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}
        return [record for _, record in sorted(self._load().items())
                if wanted.issubset(record["tags"]) and text.casefold() in record["path"].casefold()]

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

    def export_manifest(self, release, tags=(), text="", include_references=False):
        if not isinstance(release, str) or not release.strip():
            raise ValueError("release must be a non-empty string")
        release = release.strip()
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}
        if include_references:
            records = self._load_validated_with_references()
            selected = {}
            stack = [key for key, record in records.items()
                     if wanted.issubset(record["tags"])
                     and text.casefold() in record["path"].casefold()]
            while stack:
                key = stack.pop()
                if key in selected:
                    continue
                selected[key] = records[key]
                stack.extend(records[key].get("references", []))
            ordered = [selected[key] for key in sorted(selected)]
        else:
            ordered = [record for _, record in sorted(self._load_validated().items())
                       if wanted.issubset(record["tags"])
                       and text.casefold() in record["path"].casefold()]
        documents = []
        for record in ordered:
            documents.append({"path": record["path"], "name": record["name"],
                              "bytes": record["bytes"], "sha256": record["sha256"],
                              "tags": record["tags"], "status": self._file_status(record)})
        return {"release": release,
                "complete": bool(documents) and all(d["status"] == "ready" for d in documents),
                "documents": documents}

    @staticmethod
    def _validate_manifest(manifest):
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
                raise ValueError("manifest document must be a JSON object")
            path = document.get("path")
            if not isinstance(path, str):
                raise ValueError("document path must be a string")
            if (not isinstance(document.get("name"), str)
                    or not isinstance(document.get("sha256"), str)):
                raise ValueError("document name and sha256 must be strings")
            bytes_value = document.get("bytes")
            if not isinstance(bytes_value, int) or isinstance(bytes_value, bool):
                raise ValueError("document bytes must be a non-boolean integer")
            tags = document.get("tags")
            if (not isinstance(tags, list)
                    or not all(isinstance(tag, str) for tag in tags)):
                raise ValueError("document tags must be an array of strings")
            if document.get("status") not in DOCUMENT_STATUSES:
                raise ValueError("document status must be one of "
                                 + ", ".join(sorted(DOCUMENT_STATUSES)))
            if path in seen:
                raise ValueError(f"duplicate document path: {path}")
            seen.add(path)
        return documents

    @staticmethod
    def compare_manifests(before, after):
        before_documents = DocumentRoom._validate_manifest(before)
        after_documents = DocumentRoom._validate_manifest(after)
        before_by_path = {document["path"]: document for document in before_documents}
        after_by_path = {document["path"]: document for document in after_documents}
        before_paths = set(before_by_path)
        after_paths = set(after_by_path)
        added = [after_by_path[path] for path in sorted(after_paths - before_paths)]
        removed = [before_by_path[path] for path in sorted(before_paths - after_paths)]
        changed = []
        unchanged = []
        for path in sorted(before_paths & after_paths):
            old = before_by_path[path]
            new = after_by_path[path]
            fields = []
            for field in COMPARED_FIELDS:
                if field == "tags":
                    differs = set(old["tags"]) != set(new["tags"])
                else:
                    differs = old[field] != new[field]
                if differs:
                    fields.append(field)
            if fields:
                changed.append({"path": path, "before": old, "after": new,
                                "fields": sorted(fields)})
            else:
                unchanged.append(path)
        return {"before": {"release": before["release"], "complete": before["complete"]},
                "after": {"release": after["release"], "complete": after["complete"]},
                "added": added, "removed": removed,
                "changed": changed, "unchanged": unchanged}


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
    refs = commands.add_parser("refs")
    refs.add_argument("path")
    refs.add_argument("--to", action="append", default=[])
    impact = commands.add_parser("impact")
    impact.add_argument("path")
    impact.add_argument("--transitive", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("--release", default="")
    export.add_argument("--tag", action="append", default=[])
    export.add_argument("--text", default="")
    export.add_argument("--with-references", action="store_true")
    compare = commands.add_parser("compare")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    args = parser.parse_args()
    try:
        if args.command == "compare":
            before = json.loads(Path(args.before).read_text(encoding="utf-8"))
            after = json.loads(Path(args.after).read_text(encoding="utf-8"))
            result = DocumentRoom.compare_manifests(before, after)
        else:
            room = DocumentRoom(args.root, args.index)
            if args.command == "add":
                result = room.add(args.path, args.tag)
            elif args.command == "search":
                result = room.search(args.tag, args.text)
            elif args.command == "refs":
                result = room.set_references(args.path, args.to)
            elif args.command == "impact":
                result = room.reference_impact(args.path, transitive=args.transitive)
            else:
                result = room.export_manifest(args.release, args.tag, args.text,
                                              include_references=args.with_references)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
