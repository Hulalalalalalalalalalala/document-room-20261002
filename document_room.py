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

    def _load_validated_with_version_notes(self):
        records = self._load_validated_with_categories()
        for record in records.values():
            if "version_note" in record and not isinstance(record["version_note"], str):
                raise ValueError("index record version_note must be a string")
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
        if isinstance(existing, dict) and "version_note" in existing:
            record["version_note"] = existing["version_note"]
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

    def set_version_note(self, relative_path, note):
        if not isinstance(relative_path, str):
            raise ValueError("document path must be a string")
        if not isinstance(note, str):
            raise ValueError("version note must be a string")
        records = self._load_validated_with_version_notes()
        if relative_path not in records:
            raise ValueError("document is not registered")
        record = records[relative_path]
        record["version_note"] = note.strip()
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    @staticmethod
    def _normalize_tags(tags):
        return {tag.strip().lower() for tag in tags if tag.strip()}

    def edit_tags(self, paths, add_tags=(), remove_tags=()):
        if not isinstance(paths, (list, tuple)):
            raise ValueError("paths must be a list or tuple of strings")
        if not isinstance(add_tags, (list, tuple)):
            raise ValueError("add_tags must be a list or tuple of strings")
        if not isinstance(remove_tags, (list, tuple)):
            raise ValueError("remove_tags must be a list or tuple of strings")
        if (not all(isinstance(path, str) for path in paths)
                or not all(isinstance(tag, str) for tag in add_tags)
                or not all(isinstance(tag, str) for tag in remove_tags)):
            raise ValueError("paths and tags must contain only strings")
        # The whole index must be structurally sound before anything changes.
        records = self._load_validated_with_version_notes()
        unique_paths = list(dict.fromkeys(paths))  # duplicates handled once, first occurrence wins
        for path in unique_paths:
            if path not in records:
                raise ValueError("document is not registered: " + path)
        added = self._normalize_tags(add_tags)
        removed = self._normalize_tags(remove_tags)
        if added & removed:
            raise ValueError("add_tags and remove_tags overlap after normalization")
        updated = []
        unchanged = []
        for path in sorted(unique_paths):
            record = records[path]
            new_tags = sorted((self._normalize_tags(record["tags"]) - removed) | added)
            if new_tags == record["tags"]:
                unchanged.append(path)
            else:  # only the tags array changes; every other field stays as stored
                record["tags"] = new_tags
                updated.append(record)
        if updated:
            self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        return {"updated": updated,
                "unchanged": unchanged}

    def remove_record(self, relative_path, detach=False):
        if not isinstance(relative_path, str):
            raise ValueError("document path must be a string")
        if not isinstance(detach, bool):
            raise ValueError("detach must be a boolean")
        records = self._load_validated_with_categories()
        if relative_path not in records:
            raise ValueError("document is not registered")
        removed = records[relative_path]
        updated = []
        for source, record in records.items():
            if source == relative_path or "references" not in record:
                continue
            remaining = [target for target in record["references"]
                         if target != relative_path]
            # The target's own references never block its removal.
            if len(remaining) != len(record["references"]):
                if not detach:
                    raise ValueError("document is referenced by another record")
                record["references"] = remaining
                updated.append(record)
        del records[relative_path]
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"removed": removed,
                "updated": sorted(updated, key=lambda record: record["path"])}

    def relocate_record(self, source, destination):
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ValueError("source and destination paths must be strings")
        # The whole index must be structurally sound before anything changes.
        records = self._load_validated_with_version_notes()
        if source not in records:
            raise ValueError("source document is not registered")
        # The destination is resolved against the live filesystem exactly like
        # add; the source key is never resolved or read.
        try:
            path = (self.root / destination).resolve()
        except (OSError, RuntimeError, ValueError):
            raise ValueError("destination must be a file inside the document root")
        if not path.is_relative_to(self.root):
            raise ValueError("destination must be a file inside the document root")
        if not path.is_file():
            raise ValueError("destination must be a file inside the document root")
        key = path.relative_to(self.root).as_posix()
        if key in records:  # the destination key must be free, source itself included
            raise ValueError("destination document is already registered")
        original = records[source]
        content = path.read_bytes()
        if (len(content) != original["bytes"]
                or hashlib.sha256(content).hexdigest() != original["sha256"]):
            raise ValueError("destination content does not match the registered bytes or sha256")
        before = dict(original)
        after = dict(original)
        after["path"] = key
        after["name"] = path.name
        if "references" in after:  # the moved record's own self-reference follows too
            after["references"] = [key if target == source else target
                                   for target in after["references"]]
        # Every other record's references to the old key follow it to the new
        # key; order and duplicates are preserved and records without references
        # stay untouched.
        updated = []
        for other, record in records.items():
            if other == source or "references" not in record:
                continue
            if source in record["references"]:
                record["references"] = [key if target == source else target
                                        for target in record["references"]]
                updated.append(record)
        del records[source]
        records[key] = after
        self.index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        return {"before": before,
                "after": after,
                "updated": sorted(updated, key=lambda record: record["path"])}

    def relocate_records(self, items):
        # Each plan item is an object holding exactly the two string fields
        # "source" and "destination"; anything else fails the whole batch.
        if not isinstance(items, list):
            raise ValueError("relocation batch must be a list")
        parsed = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"source", "destination"}:
                raise ValueError(
                    "each relocation must be an object with exactly source and destination")
            source = item["source"]
            destination = item["destination"]
            if not isinstance(source, str) or not isinstance(destination, str):
                raise ValueError("source and destination paths must be strings")
            parsed.append((source, destination))
        # The whole index must be structurally sound before anything changes.
        records = self._load_validated_with_version_notes()
        sources = []
        for source, _ in parsed:
            if source not in records:
                raise ValueError("source document is not registered: " + source)
            sources.append(source)
        if len(set(sources)) != len(sources):
            raise ValueError("relocation batch contains duplicate sources")
        moving = set(sources)
        # Every destination is resolved against the live filesystem exactly
        # like add; source keys are never resolved or read. All destinations
        # are checked (and later read) in plan order before anything changes,
        # so the outcome never depends on that order.
        pairs = []
        seen_destinations = set()
        for source, destination in parsed:
            try:
                path = (self.root / destination).resolve()
            except (OSError, RuntimeError, ValueError):
                raise ValueError("destination must be a file inside the document root")
            if not path.is_relative_to(self.root):  # escaping targets are never read
                raise ValueError("destination must be a file inside the document root")
            if not path.is_file():
                raise ValueError("destination must be a file inside the document root")
            key = path.relative_to(self.root).as_posix()
            if key == source:
                raise ValueError("destination must differ from the source path")
            if key in seen_destinations:
                raise ValueError("relocation batch contains duplicate destinations")
            seen_destinations.add(key)
            # A key held by a record that does not move is forbidden; a key
            # belonging to another source in this batch allows swaps and
            # multi-document relocation cycles.
            if key in records and key not in moving:
                raise ValueError("destination document is already registered")
            pairs.append((source, key, path))
        # The file waiting at each target must carry the source record's
        # registered bytes and digest; content metadata is never refreshed.
        for source, key, path in pairs:
            original = records[source]
            content = path.read_bytes()
            if (len(content) != original["bytes"]
                    or hashlib.sha256(content).hexdigest() != original["sha256"]):
                raise ValueError(
                    "destination content does not match the registered bytes or sha256")
        mapping = {source: key for source, key, _ in pairs}
        after_by_source = {}
        moved = []
        for source, key, path in pairs:
            original = records[source]
            before = dict(original)
            after = dict(original)
            after["path"] = key
            after["name"] = path.name
            if "references" in after:  # every old key is remapped in one step
                after["references"] = [mapping.get(target, target)
                                       for target in after["references"]]
            after_by_source[source] = after
            moved.append({"before": before, "after": after})
        # Every other record's references follow the moved documents to their
        # new keys; one simultaneous mapping pass keeps swaps and cycles
        # correct. Order and duplicates are preserved and records without
        # references stay untouched.
        updated = []
        new_records = {}
        for old_key, record in records.items():
            if old_key in moving:
                continue
            if "references" in record and any(
                    target in mapping for target in record["references"]):
                record["references"] = [mapping.get(target, target)
                                        for target in record["references"]]
                updated.append(record)
            new_records[old_key] = record
        for source, key, _ in pairs:
            new_records[key] = after_by_source[source]
        if pairs:  # an empty batch validates the index but never writes it
            self.index.write_text(
                json.dumps(new_records, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        return {"moved": sorted(moved, key=lambda item: item["before"]["path"]),
                "updated": sorted(updated, key=lambda record: record["path"])}

    @staticmethod
    def _validate_importable(records):
        # Per-record field and type checks shared by the stored index and an
        # import batch; reference targets are resolved separately, against the
        # merged index.
        for key, record in records.items():
            if (not isinstance(key, str) or not isinstance(record, dict)
                    or record.get("path") != key):
                raise ValueError("records must be objects keyed by their path")
            if (not isinstance(record.get("name"), str)
                    or not isinstance(record.get("bytes"), int)
                    or isinstance(record.get("bytes"), bool)
                    or not isinstance(record.get("sha256"), str)
                    or not isinstance(record.get("tags"), list)
                    or not all(isinstance(tag, str) for tag in record["tags"])):
                raise ValueError("record has invalid field types")
            if "archived" in record and not isinstance(record["archived"], bool):
                raise ValueError("record archived must be a boolean")
            references = record.get("references", [])
            if (not isinstance(references, list)
                    or not all(isinstance(target, str) for target in references)):
                raise ValueError("record references must be a list of strings")
            if "category" in record and not isinstance(record["category"], str):
                raise ValueError("record category must be a string")

    @staticmethod
    def _check_replace_paths(replace_paths):
        # Only a string list or tuple is accepted; the paths are exact,
        # case-sensitive index keys and are never trimmed, resolved or parsed.
        if not isinstance(replace_paths, (list, tuple)):
            raise ValueError("replace_paths must be a list or tuple of strings")
        if not all(isinstance(path, str) for path in replace_paths):
            raise ValueError("replace_paths must contain only strings")

    def _prepare_import(self, batch, replace_paths=()):
        if not isinstance(batch, dict):
            raise ValueError("import batch must be an object of records")
        existing = self._load()
        if not isinstance(existing, dict):
            raise ValueError("index must be a JSON object of records")
        # Bad records fail the whole batch even when their paths are not imported.
        self._validate_importable(existing)
        self._validate_importable(batch)
        replace_set = set(replace_paths)
        # Every approved path must be a conflicting candidate: present on both
        # sides. Identical pairs stay unchanged even when listed.
        for path in replace_paths:
            if path not in existing or path not in batch:
                raise ValueError("replace path is not in both the index and the batch: "
                                 + path)
        return existing, replace_set

    @staticmethod
    def _import_field_differences(existing, incoming):
        # Top-level fields that are new, missing or value-different. Every
        # nested difference is attributed to its top-level field, and equality
        # follows the import rules: array order is significant, object key
        # order is not, and a missing optional field differs from its
        # explicit default.
        fields = set(existing) ^ set(incoming)
        fields.update(field for field in existing.keys() & incoming.keys()
                      if existing[field] != incoming[field])
        return sorted(fields)

    def preview_import(self, batch, replace_paths=()):
        self._check_replace_paths(replace_paths)
        existing, replace_set = self._prepare_import(batch, replace_paths)
        added = []
        unchanged = []
        conflicts = []
        replaced = []
        for key in sorted(set(existing) | set(batch)):
            if key not in existing:
                added.append(key)
            elif key not in batch:
                continue
            elif existing[key] == batch[key]:  # field set and values, arrays ordered
                unchanged.append(key)
            else:
                item = {"path": key, "existing": existing[key],
                        "incoming": batch[key],
                        "fields": self._import_field_differences(
                            existing[key], batch[key])}
                # An approved differing path is replaced instead of conflicting;
                # the item shape is identical either way.
                if key in replace_set:
                    replaced.append(item)
                else:
                    conflicts.append(item)
        # Reference targets are resolved against the union of registered paths
        # and batch paths, checked on every record of both sides so a
        # conflicting or replaced record can neither introduce nor hide a
        # dangling target.
        for records in (existing, batch):
            for record in records.values():
                for target in record.get("references", []):
                    if target not in existing and target not in batch:
                        raise ValueError("record references an unregistered path")
        result = {"can_import": not conflicts,
                  "added": added, "unchanged": unchanged, "conflicts": conflicts}
        if replace_set:  # only a non-empty replace list gains the extra category
            result["replaced"] = replaced
        return result

    def import_records(self, batch, replace_paths=()):
        self._check_replace_paths(replace_paths)
        existing, replace_set = self._prepare_import(batch, replace_paths)
        added = []
        unchanged = []
        replaced = []
        for key, record in batch.items():
            if key not in existing:
                added.append(key)
            elif existing[key] == record:  # field set and values, arrays ordered
                unchanged.append(key)
            elif key in replace_set:
                # The incoming complete record replaces the stored one; fields
                # are never merged and array order is kept verbatim.
                replaced.append(key)
            else:
                raise ValueError("import conflicts with the existing record: " + key)
        # Reference targets are resolved against the union of registered paths
        # and batch paths, checked on every record of both sides so replacing a
        # record can never hide a dangling target on the record it replaces.
        for records in (existing, batch):
            for record in records.values():
                for target in record.get("references", []):
                    if target not in existing and target not in batch:
                        raise ValueError("record references an unregistered path")
        merged = dict(existing)
        for key in added:
            merged[key] = batch[key]
        for key in replaced:
            merged[key] = batch[key]
        if added or replaced:
            self.index.parent.mkdir(parents=True, exist_ok=True)
            self.index.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        result = {"added": sorted(added), "unchanged": sorted(unchanged)}
        if replace_set:  # only a non-empty replace list gains the extra category
            result["replaced"] = sorted(replaced)
        return result

    def export_records(self, tags=(), text="", archive_state="all", category=None):
        if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("tags must be a list or tuple of strings")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        self._check_archive_state(archive_state)
        self._check_category_filter(category)
        records = self._load_validated_with_categories()
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}

        def matches(record):
            return (wanted.issubset(record["tags"])
                    and text.casefold() in record["path"].casefold()
                    and self._matches_archive_state(record, archive_state)
                    and self._matches_category(record, category))

        selected = {}
        stack = [key for key, record in records.items() if matches(record)]
        while stack:
            key = stack.pop()
            if key in selected:
                continue
            selected[key] = records[key]
            # Dependencies are pulled in regardless of the filters; records
            # that merely reference a selected one are not.
            stack.extend(records[key].get("references", []))
        return {key: selected[key] for key in sorted(selected)}

    def reference_impact(self, target, transitive=False, include_routes=False):
        if not isinstance(target, str):
            raise ValueError("target path must be a string")
        if not isinstance(transitive, bool):
            raise ValueError("transitive must be a boolean")
        if not isinstance(include_routes, bool):
            raise ValueError("include_routes must be a boolean")
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
        routes = {}
        if include_routes:
            # One shortest chain per referrer, from the referrer to the target.
            # Built level by level so every next hop's best suffix is already
            # known; equal-length chains tie-break on the case-sensitive
            # lexicographic order of the full path array.
            for key in sorted(distances, key=lambda key: (distances[key], key)):
                suffixes = []
                for referenced in records[key].get("references", []):
                    if referenced == target and distances[key] == 1:
                        suffixes.append([target])
                    elif (referenced in distances
                          and distances[referenced] == distances[key] - 1):
                        suffixes.append(routes[referenced])
                routes[key] = [key] + min(suffixes)
        documents = []
        for key in sorted(distances, key=lambda key: (distances[key], key)):
            document = {**records[key], "distance": distances[key]}
            if include_routes:
                document["route"] = routes[key]
            documents.append(document)
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

    def find_reference_cycles(self, tags=(), text="", archive_state="all", category=None):
        if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("tags must be a list or tuple of strings")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        self._check_archive_state(archive_state)
        self._check_category_filter(category)
        # The cycle structure is computed from the whole index first, so every
        # stored record — including records no group will return — is validated.
        records = self._load_validated_with_version_notes()
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}

        def matches(path):
            record = records[path]
            return (wanted.issubset(record["tags"])
                    and text.casefold() in path.casefold()
                    and self._matches_archive_state(record, archive_state)
                    and self._matches_category(record, category))

        # Tarjan's algorithm gives the maximal sets in which every two distinct
        # paths reach each other along reference edges: rings sharing a member
        # become one group, independent rings joined only by one-way edges do
        # not, and a document that only points at or is only pointed at by a
        # ring never joins it. An explicit work stack keeps long one-way chains
        # from depending on the Python recursion limit, and a fixed iteration
        # order over sorted path keys keeps the output independent of index or
        # references array order.
        paths = sorted(records)
        successors = {path: records[path].get("references", []) for path in paths}
        index_of = {}
        lowlink = {}
        stack = []
        on_stack = set()
        counter = 0
        components = []
        for start in paths:
            if start in index_of:
                continue
            index_of[start] = lowlink[start] = counter
            counter += 1
            stack.append(start)
            on_stack.add(start)
            work = [(start, 0)]
            while work:
                node, edge_index = work[-1]
                targets = successors[node]
                if edge_index < len(targets):
                    target = targets[edge_index]
                    work[-1] = (node, edge_index + 1)
                    if target not in index_of:
                        index_of[target] = lowlink[target] = counter
                        counter += 1
                        stack.append(target)
                        on_stack.add(target)
                        work.append((target, 0))
                    elif target in on_stack:
                        lowlink[node] = min(lowlink[node], index_of[target])
                else:
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        lowlink[parent] = min(lowlink[parent], lowlink[node])
                    if lowlink[node] == index_of[node]:
                        members = []
                        while True:
                            member = stack.pop()
                            on_stack.remove(member)
                            members.append(member)
                            if member == node:
                                break
                        components.append(members)
        groups = []
        for members in components:
            if len(members) == 1:
                member = members[0]
                # A lone record forms a group only when it references itself.
                if member not in successors[member]:
                    continue
            ordered = sorted(members)
            # A filter hitting any single member returns the whole group; the
            # other members are not required to match.
            matched = [path for path in ordered if matches(path)]
            if not matched:
                continue
            groups.append({"matched": matched,
                           "documents": [records[path] for path in ordered]})
        groups.sort(key=lambda group: group["documents"][0]["path"])
        return {"groups": groups}

    def merge_duplicates(self, keep, sources):
        if not isinstance(keep, str):
            raise ValueError("keep path must be a string")
        if (not isinstance(sources, (list, tuple)) or not sources
                or not all(isinstance(source, str) for source in sources)):
            raise ValueError("sources must be a non-empty list or tuple of strings")
        # The whole index must be structurally sound before anything changes.
        records = self._load_validated_with_version_notes()
        if keep not in records:
            raise ValueError("keep document is not registered")
        unique_sources = list(dict.fromkeys(sources))  # duplicates handled once
        if keep in unique_sources:
            raise ValueError("sources must not contain the keep path")
        for source in unique_sources:
            if source not in records:
                raise ValueError("source document is not registered: " + source)
        kept = records[keep]
        for source in unique_sources:
            record = records[source]
            # Content metadata compares as stored: the digest's exact string.
            if record["bytes"] != kept["bytes"] or record["sha256"] != kept["sha256"]:
                raise ValueError("source content metadata does not match the keep record")
        merging = set(unique_sources)
        participants = [keep] + unique_sources
        # The keep record collects its own and every source's original direct
        # references, source paths replaced by the keep path, deduplicated and
        # sorted case-sensitively. The field is stored only when at least one
        # participant originally carried it.
        after = dict(kept)
        if any("references" in records[path] for path in participants):
            after["references"] = sorted({
                keep if target in merging else target
                for path in participants
                for target in records[path].get("references", [])})
        removed = [dict(records[source]) for source in unique_sources]
        # Every other surviving record's references to a source follow it to
        # the keep path; order and duplicates are preserved and records
        # without references stay untouched.
        updated = []
        new_records = {}
        for key, record in records.items():
            if key in merging:
                continue
            if key == keep:
                new_records[key] = after
                continue
            if "references" in record and any(
                    target in merging for target in record["references"]):
                record["references"] = [keep if target in merging else target
                                        for target in record["references"]]
                updated.append(record)
            new_records[key] = record
        self.index.write_text(json.dumps(new_records, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        return {"kept": after,
                "removed": sorted(removed, key=lambda record: record["path"]),
                "updated": sorted(updated, key=lambda record: record["path"])}

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

    @staticmethod
    def _release_blockers(records, starts, status_by_path):
        # Reverse reference edges, used to reconstruct shortest routes after
        # the breadth-first distances are known.
        referrers = {}
        for source, record in records.items():
            for target in record.get("references", []):
                referrers.setdefault(target, set()).add(source)
        blockers = []
        for start in sorted(starts):
            distances = {start: 0}
            frontier = [start]
            while frontier:  # abnormal intermediate nodes never stop the walk
                nxt = []
                for node in frontier:
                    for target in records[node].get("references", []):
                        if target not in distances:
                            distances[target] = distances[node] + 1
                            nxt.append(target)
                frontier = nxt
            routes = {start: [start]}
            abnormal = []
            # Nodes by rising depth keep every predecessor's best route known.
            for key in sorted(distances, key=lambda key: (distances[key], key)):
                if key != start:
                    # Equal-length chains tie-break on the case-sensitive
                    # lexicographic order of the full path array.
                    routes[key] = min(
                        routes[predecessor] + [key]
                        for predecessor in referrers.get(key, ())
                        if distances.get(predecessor) == distances[key] - 1)
                if status_by_path[key] != "ready":
                    abnormal.append(key)
            if abnormal:  # starts without a reachable abnormal document are omitted
                blockers.append({"path": start, "documents": [
                    {"path": key, "status": status_by_path[key],
                     "distance": distances[key], "route": routes[key]}
                    for key in abnormal]})
        return blockers

    def export_manifest(self, release, tags=(), text="", include_references=False,
                        archive_state="all", category=None, include_origins=False,
                        include_version_notes=False, include_blockers=False):
        if not isinstance(release, str) or not release.strip():
            raise ValueError("release must be a non-empty string")
        self._check_archive_state(archive_state)
        self._check_category_filter(category)
        if not isinstance(include_origins, bool):
            raise ValueError("include_origins must be a boolean")
        if include_origins and not include_references:
            raise ValueError("include_origins requires include_references")
        if not isinstance(include_version_notes, bool):
            raise ValueError("include_version_notes must be a boolean")
        if not isinstance(include_blockers, bool):
            raise ValueError("include_blockers must be a boolean")
        if include_blockers and not include_references:
            raise ValueError("include_blockers requires include_references")
        release = release.strip()
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}

        def matches(record):
            return (wanted.issubset(record["tags"])
                    and text.casefold() in record["path"].casefold()
                    and self._matches_archive_state(record, archive_state)
                    and self._matches_category(record, category))

        if include_version_notes:
            records = self._load_validated_with_version_notes()
        elif include_references or archive_state != "all" or category is not None:
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
            if include_version_notes:
                # Records written before this feature lack the field and
                # export an empty note; referenced documents keep their own.
                document["version_note"] = record.get("version_note", "")
            documents.append(document)
        manifest = {"release": release,
                    "complete": bool(documents) and all(d["status"] == "ready" for d in documents),
                    "documents": documents}
        if include_blockers:
            # Only starting documents carry a blockers entry, and only when
            # themselves or a reachable reference is not ready.
            status_by_path = {document["path"]: document["status"] for document in documents}
            manifest["blockers"] = self._release_blockers(records, starts, status_by_path)
        return manifest

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
    def _changed_fields(old, new, compare_version_notes=False):
        fields = []
        for field in DocumentRoom.COMPARED_FIELDS:
            if field == "tags":
                differs = set(old["tags"]) != set(new["tags"])
            else:
                differs = old[field] != new[field]
            if differs:
                fields.append(field)
        if compare_version_notes:
            # Notes compare as the exact stored strings; a missing field is
            # equivalent to an empty note. Appended last, the field list stays
            # sorted by name.
            if old.get("version_note", "") != new.get("version_note", ""):
                fields.append("version_note")
        return fields

    @staticmethod
    def _check_manifest_version_notes(manifest):
        for document in manifest["documents"]:
            if "version_note" in document and not isinstance(document["version_note"], str):
                raise ValueError("manifest document version_note must be a string")

    @staticmethod
    def _detect_relocations(result, compare_version_notes=False):
        # Only the leftover added/removed documents are candidates; documents
        # paired by path never count toward uniqueness. Candidates group by
        # the exact stored bytes and sha256 (the digest's case is significant),
        # and a group merges only when both sides hold exactly one document.
        removed_by_content = {}
        for document in result["removed"]:
            removed_by_content.setdefault(
                (document["bytes"], document["sha256"]), []).append(document)
        added_by_content = {}
        for document in result["added"]:
            added_by_content.setdefault(
                (document["bytes"], document["sha256"]), []).append(document)
        relocated = []
        for content_key, old_docs in removed_by_content.items():
            new_docs = added_by_content.get(content_key, [])
            if len(old_docs) != 1 or len(new_docs) != 1:
                continue
            old, new = old_docs[0], new_docs[0]
            relocated.append({"before": old, "after": new,
                              "fields": sorted(["path"]
                                               + DocumentRoom._changed_fields(
                                                   old, new, compare_version_notes))})
        relocated.sort(key=lambda item: (item["before"]["path"], item["after"]["path"]))
        moved_before = {item["before"]["path"] for item in relocated}
        moved_after = {item["after"]["path"] for item in relocated}
        result["removed"] = [d for d in result["removed"]
                             if d["path"] not in moved_before]
        result["added"] = [d for d in result["added"]
                           if d["path"] not in moved_after]
        return relocated

    @staticmethod
    def compare_manifests(before, after, detect_relocations=False,
                          compare_version_notes=False):
        if not isinstance(detect_relocations, bool):
            raise ValueError("detect_relocations must be a boolean")
        if not isinstance(compare_version_notes, bool):
            raise ValueError("compare_version_notes must be a boolean")
        before = DocumentRoom._validated_manifest(before)
        after = DocumentRoom._validated_manifest(after)
        if compare_version_notes:
            # Every document on both sides is checked, paired or not, before
            # any comparison result is produced.
            DocumentRoom._check_manifest_version_notes(before)
            DocumentRoom._check_manifest_version_notes(after)
        before_docs = {d["path"]: d for d in before["documents"]}
        after_docs = {d["path"]: d for d in after["documents"]}
        before_paths = set(before_docs)
        after_paths = set(after_docs)
        changed = []
        unchanged = []
        for path in sorted(before_paths & after_paths):
            old = before_docs[path]
            new = after_docs[path]
            fields = DocumentRoom._changed_fields(old, new, compare_version_notes)
            if fields:
                changed.append({"path": path, "before": old, "after": new,
                                "fields": fields})
            else:
                unchanged.append(path)
        result = {
            "before": {"release": before["release"], "complete": before["complete"]},
            "after": {"release": after["release"], "complete": after["complete"]},
            "added": [after_docs[path] for path in sorted(after_paths - before_paths)],
            "removed": [before_docs[path] for path in sorted(before_paths - after_paths)],
            "changed": changed,
            "unchanged": unchanged,
        }
        if detect_relocations:
            result["relocated"] = DocumentRoom._detect_relocations(
                result, compare_version_notes)
        return result

    @staticmethod
    def compare_manifest_files(before_path, after_path, detect_relocations=False,
                               compare_version_notes=False):
        if not isinstance(detect_relocations, bool):
            raise ValueError("detect_relocations must be a boolean")
        if not isinstance(compare_version_notes, bool):
            raise ValueError("compare_version_notes must be a boolean")
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
        return DocumentRoom.compare_manifests(
            before, after, detect_relocations=detect_relocations,
            compare_version_notes=compare_version_notes)


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
    note = commands.add_parser("note")
    note.add_argument("path")
    note.add_argument("--value", required=True)
    remove = commands.add_parser("remove")
    remove.add_argument("path")
    remove.add_argument("--detach", action="store_true")
    tag_edit = commands.add_parser("tags")
    tag_edit.add_argument("paths", nargs="*")
    tag_edit.add_argument("--add", action="append", default=[])
    tag_edit.add_argument("--remove", action="append", default=[])
    relocate = commands.add_parser("relocate")
    relocate.add_argument("source")
    relocate.add_argument("destination")
    relocate_batch = commands.add_parser("relocate-batch")
    relocate_batch.add_argument("--from", dest="from_path", required=True)
    refs = commands.add_parser("refs")
    refs.add_argument("path")
    refs.add_argument("--to", action="append", default=[])
    impact = commands.add_parser("impact")
    impact.add_argument("path")
    impact.add_argument("--transitive", action="store_true")
    impact.add_argument("--with-routes", action="store_true")
    duplicates = commands.add_parser("duplicates")
    duplicates.add_argument("--tag", action="append", default=[])
    duplicates.add_argument("--text", default="")
    cycles = commands.add_parser("reference-cycles")
    cycles.add_argument("--tag", action="append", default=[])
    cycles.add_argument("--text", default="")
    cycles.add_argument("--archive-state", default="all")
    cycles.add_argument("--category", default=None)
    merge = commands.add_parser("merge-duplicates")
    merge.add_argument("path")
    merge.add_argument("--from", dest="sources", action="append", default=[])
    export = commands.add_parser("export")
    export.add_argument("--release", default="")
    export.add_argument("--tag", action="append", default=[])
    export.add_argument("--text", default="")
    export.add_argument("--with-references", action="store_true")
    export.add_argument("--with-origins", action="store_true")
    export.add_argument("--with-version-notes", action="store_true")
    export.add_argument("--with-blockers", action="store_true")
    export.add_argument("--archive-state", default="all")
    export.add_argument("--category", default=None)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--detect-relocations", action="store_true")
    compare.add_argument("--compare-version-notes", action="store_true")
    importer = commands.add_parser("import")
    importer.add_argument("--from", dest="from_path", required=True)
    importer.add_argument("--preview", action="store_true")
    importer.add_argument("--replace", action="append", default=[])
    dump = commands.add_parser("dump")
    dump.add_argument("--tag", action="append", default=[])
    dump.add_argument("--text", default="")
    dump.add_argument("--archive-state", default="all")
    dump.add_argument("--category", default=None)
    args = parser.parse_args()
    try:
        room = DocumentRoom(args.root, args.index)
        if args.command == "add":
            result = room.add(args.path, args.tag)
        elif args.command == "archive":
            result = room.set_archived(args.path, not args.restore)
        elif args.command == "category":
            result = room.set_category(args.path, args.value)
        elif args.command == "note":
            result = room.set_version_note(args.path, args.value)
        elif args.command == "remove":
            result = room.remove_record(args.path, detach=args.detach)
        elif args.command == "tags":
            if not args.paths:
                raise ValueError("at least one path is required")
            result = room.edit_tags(args.paths, add_tags=args.add,
                                    remove_tags=args.remove)
        elif args.command == "relocate":
            result = room.relocate_record(args.source, args.destination)
        elif args.command == "relocate-batch":
            try:
                with open(args.from_path, encoding="utf-8") as handle:
                    batch = json.load(handle)
            except UnicodeDecodeError as exc:
                raise ValueError(f"relocation plan file is not valid UTF-8: {exc}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"relocation plan file is not valid JSON: {exc}")
            result = room.relocate_records(batch)
        elif args.command == "search":
            result = room.search(args.tag, args.text, args.archive_state, args.category)
        elif args.command == "refs":
            result = room.set_references(args.path, args.to)
        elif args.command == "impact":
            result = room.reference_impact(args.path, transitive=args.transitive,
                                           include_routes=args.with_routes)
        elif args.command == "duplicates":
            result = room.find_duplicates(args.tag, args.text)
        elif args.command == "reference-cycles":
            result = room.find_reference_cycles(args.tag, args.text,
                                               args.archive_state, args.category)
        elif args.command == "merge-duplicates":
            result = room.merge_duplicates(args.path, args.sources)
        elif args.command == "compare":
            result = room.compare_manifest_files(
                args.before, args.after,
                detect_relocations=args.detect_relocations,
                compare_version_notes=args.compare_version_notes)
        elif args.command == "import":
            try:
                with open(args.from_path, encoding="utf-8") as handle:
                    batch = json.load(handle)
            except UnicodeDecodeError as exc:
                raise ValueError(f"import file is not valid UTF-8: {exc}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"import file is not valid JSON: {exc}")
            result = (room.preview_import(batch, args.replace) if args.preview
                      else room.import_records(batch, args.replace))
        elif args.command == "dump":
            result = room.export_records(args.tag, args.text, args.archive_state,
                                         args.category)
        else:
            result = room.export_manifest(args.release, args.tag, args.text,
                                          include_references=args.with_references,
                                          archive_state=args.archive_state,
                                          category=args.category,
                                          include_origins=args.with_origins,
                                          include_version_notes=args.with_version_notes,
                                          include_blockers=args.with_blockers)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
