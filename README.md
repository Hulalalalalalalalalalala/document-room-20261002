# Document Room

A local file metadata index. Python 3.10+ and the standard library are sufficient.

Run `python3 document_room.py add policy.md --tag legal --tag policy` from this directory to index the sample policy. Add the second sample with `python3 document_room.py add meeting.txt --tag operations`. `python3 document_room.py search --tag legal --text policy` returns matching metadata as JSON. Multiple tags are combined with AND; tags are trimmed and lowercase, and text matches the relative path without case sensitivity.

The default document root is `samples`; metadata is persisted in `.state/documents.json`. Override these with `--root` and `--index` before the subcommand. Re-adding a file refreshes its size, SHA-256 and tags. Search reads the stored metadata, not live file contents. Files must resolve inside the document root. This version does not copy documents, extract content or watch filesystem changes. Use the Python `DocumentRoom.add` and `DocumentRoom.search` methods for the same operations. CLI errors return JSON and exit 2.

## Release manifests

`python3 document_room.py export --release "Q4 Launch" --tag legal --text policy` checks a release against the live files and prints `{"release", "complete", "documents"}`. The release name is required and trimmed; blank or non-string names raise `ValueError` from `DocumentRoom.export_manifest` (the CLI prints a JSON error and exits 2). `--tag` (repeatable, AND-combined) and `--text` (case-insensitive path substring) filter records with the same normalization as `search`. Documents are sorted by recorded relative path and keep their stored `path`, `name`, `bytes`, `sha256` and `tags`, plus a `status`:

- `ready` — present, and both byte count and SHA-256 match the index;
- `changed` — present but the size or digest differs;
- `missing` — absent, a dangling symlink, or now a directory;
- `unsafe` — the resolved path escapes the document root (the target is never read);
- `unreadable` — any other resolution or read failure.

Stored metadata is never overwritten with current values, and one document failing does not stop the others. `complete` is true only when the manifest is non-empty and every document is `ready`; an empty manifest still contains all three top-level fields. A missing index yields an empty manifest and is not created; malformed JSON or records whose structure/types do not match the registered format raise `ValueError`, and read failures raise `OSError`. Exporting only reads the index and files: it never copies documents, writes a manifest file or updates the index.

## References

`python3 document_room.py refs policy.md --to meeting.txt` records that a registered document directly refers to other registered documents, and `DocumentRoom.set_references(relative_path, targets)` does the same from Python. The source path is the positional argument and `--to` is repeatable; omitting `--to` clears the references. Paths match the index's relative-path keys exactly (case-sensitive) and are never resolved against the filesystem. Targets are deduplicated, sorted by path and stored as a `references` array on the record — an empty list is stored as `[]`. Self-references and mutual references are allowed. The updated record is returned as JSON and the CLI exits 0.

A non-string source, an unregistered source or target, or targets that are not a list of strings raise `ValueError` without changing the index; a missing index is treated as empty and is not created. Corrupt JSON, records that do not match the registered format, a stored `references` field that is not a list of strings, or a stored reference to an unregistered path also raise `ValueError`, and index read/write failures raise `OSError`. Records written before this feature have no `references` field and are treated as having none: the first `add` of a path stores only the original fields, re-adding the same path keeps its references, and `search` returns any saved references alongside the other metadata.

`python3 document_room.py export --release "Q4 Launch" --with-references` (or `export_manifest(..., include_references=True)`) expands the manifest: the usual tag and text filters pick the starting documents, then every referenced document is pulled in recursively — references are included whether or not they match the filters, each path appears once, and cycles still terminate. The manifest keeps the existing top-level and document fields sorted by path, every listed file is checked with the same status rules, and `complete` is false when any document is not `ready` or the manifest is empty. Reference-aware exports validate stored references as above; without the flag, export behaves exactly as before.

## Reference impact

`python3 document_room.py impact meeting.txt` answers the reverse question — which registered documents use a given document — and `DocumentRoom.reference_impact(target, transitive=False)` does the same from Python. The positional path identifies the target with the same exact, case-sensitive relative-path key as `refs`; it is never resolved against the filesystem. By default only direct referrers are returned; pass `--transitive` (or `transitive=True`) to also include every document that reaches the target through one or more reference edges. The command prints `{"path", "documents"}` and exits 0, where `path` echoes the input key.

Each document is the same record `search` returns plus an integer `distance`: the minimum number of reference edges from that document to the target. Direct referrers always have distance 1; transitive results add documents at greater distances. Results are sorted by distance ascending and then by relative path lexicographically, each path appears once, and the target itself is always excluded even when it references itself. Self-references and reference cycles stay legal and the query always terminates. A registered target that nobody references yields an empty `documents` array.

The impact query reads the stored index only: missing, changed or unreadable document files do not affect the result and no live file state is checked, and the index is never modified or created. A non-string or unregistered target, or a non-boolean `transitive`, raises `ValueError`; a missing index is treated as empty (so any target is unregistered and no file is created). Corrupt JSON, records that do not match the registered format, a `references` field that is not a list of strings, or a reference to an unregistered path also raises `ValueError` — even when the offending record is unrelated to the target — and index read failures raise `OSError`. The CLI reports these as a JSON object with an `error` field and exit code 2. Old records without a `references` field are treated as having none, and the field is not added.

## Manifest comparison

`python3 document_room.py compare --before old.json --after new.json` diffs two manifests previously produced by `export` (plain UTF-8 JSON files), and `DocumentRoom.compare_manifests(before, after)` does the same from Python with the manifest dictionaries; both paths give identical results. The comparison relies solely on the manifest snapshots: the inputs and index are never modified, no document files are read, and nothing is created. The two manifests may use the same release name or different filter selections.

The result is `{"before", "after", "added", "removed", "changed", "unchanged"}`. `before` and `after` echo each input's `release` and `complete` (release/complete differences never affect document classification and `complete` is never recomputed). `added` lists complete documents present only in the second manifest and `removed` those present only in the first; `changed` lists `{"path", "before", "after", "fields"}` keeping both full documents plus the changed field names; `unchanged` lists paths with no compared change.

Documents pair by case-sensitive exact `path` (no normalization); only `name`, `bytes`, `sha256`, `tags` and `status` are compared. Tags compare as string sets, ignoring order and duplicates (but not case or whitespace); the other fields compare by value, so a `status`-only change still counts as changed. Extra document fields are preserved in the returned documents but never compared. Each category is sorted by path and `fields` is sorted by field name; each path appears exactly once. Two empty manifests yield four empty categories.

Each input must be an object with a non-blank string `release`, a boolean `complete` and an array `documents`; every document needs string `path`, `name` and `sha256`, a non-boolean integer `bytes`, an array of strings `tags`, and one of the five statuses, with no duplicate paths within a manifest. Extra fields are allowed. Bad structure or types, duplicate paths, invalid statuses, and JSON or UTF-8 decode errors raise `ValueError` (the CLI prints a JSON error and exits 2); unreadable files raise `OSError`.

Run `python3 -B -m unittest -v` for the API, path-boundary and CLI tests.
