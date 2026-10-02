"""A local document metadata index with conjunctive tag search and release manifests."""
import argparse
import hashlib
import json
from pathlib import Path


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
        referrers = {key: [] for key in records}
        for source, record in records.items():
            for referenced in record.get("references", []):
                referrers[referenced].append(source)
        distances = {}
        frontier = [target]
        edge = 0
        while frontier:
            edge += 1
            upcoming = []
            for node in frontier:
                for source in referrers[node]:
                    if source != target and source not in distances:
                        distances[source] = edge
                        upcoming.append(source)
            if not transitive:
                break
            frontier = upcoming
        documents = []
        for key in sorted(distances, key=lambda candidate: (distances[candidate], candidate)):
            item = dict(records[key])
            item["distance"] = distances[key]
            documents.append(item)
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
    args = parser.parse_args()
    try:
        room = DocumentRoom(args.root, args.index)
        if args.command == "add":
            result = room.add(args.path, args.tag)
        elif args.command == "search":
            result = room.search(args.tag, args.text)
        elif args.command == "refs":
            result = room.set_references(args.path, args.to)
        elif args.command == "impact":
            result = room.reference_impact(args.path, args.transitive)
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
