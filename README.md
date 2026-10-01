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


Run `python3 -B -m unittest -v` for the API, path-boundary and CLI tests.
