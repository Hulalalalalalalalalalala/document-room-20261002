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

`python3 document_room.py refs policy.md --to meeting.txt` replaces the direct references of a registered document and prints the full updated record as JSON. `--to` is repeatable; omitting it clears the references, which are stored as an empty `references` array. Targets are deduplicated and stored sorted by path. Both the source path and the targets are matched case-sensitively against the index's relative-path keys — the filesystem is never consulted — so the source and every target must already be registered. Self-references and mutual references are allowed. `DocumentRoom.set_references(relative_path, targets)` is the Python entry point: a non-string source, an unregistered source or target, or targets that are not a list of strings raise `ValueError` without touching the index; a missing index is treated as empty and is not created. Corrupt JSON, records that do not match the registered format, stored `references` that are not a list of strings, or stored references to unregistered documents also raise `ValueError`, and index read/write failures raise `OSError`. Records written before references existed are treated as having none; re-adding a path preserves its stored references, and `search` returns them with each record.

`python3 document_room.py export --release R1 --with-references` (Python: `export_manifest(..., include_references=True)`, off by default) starts from the documents matching the `--tag`/`--text` filters and recursively adds every referenced document, whether or not the targets match the filters themselves. Each path appears once, reference cycles still terminate, and the manifest stays sorted by path with the same top-level and document fields; every listed document is checked against its real file with the usual status rules, and `complete` is false if any document is not `ready` or the manifest is empty. With references enabled, export applies the same index validation and exceptions as `refs`; with them disabled, export behaves exactly as before.


Run `python3 -B -m unittest -v` for the API, path-boundary and CLI tests.
