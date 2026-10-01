# Document Room

A local file metadata index. Python 3.10+ and the standard library are sufficient.

Run `python3 document_room.py add policy.md --tag legal --tag policy` from this directory to index the sample policy. Add the second sample with `python3 document_room.py add meeting.txt --tag operations`. `python3 document_room.py search --tag legal --text policy` returns matching metadata as JSON. Multiple tags are combined with AND; tags are trimmed and lowercase, and text matches the relative path without case sensitivity.

The default document root is `samples`; metadata is persisted in `.state/documents.json`. Override these with `--root` and `--index` before the subcommand. Re-adding a file refreshes its size, SHA-256 and tags. Search reads the stored metadata, not live file contents. Files must resolve inside the document root. This version does not copy documents, extract content or watch filesystem changes. Use the Python `DocumentRoom.add` and `DocumentRoom.search` methods for the same operations. CLI errors return JSON and exit 2.

Run `python3 -B -m unittest -v` for the API, path-boundary and CLI tests.
