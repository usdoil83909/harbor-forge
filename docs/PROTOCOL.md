# Protocol reference

This document describes the implementation in [main.py](../main.py). All surfaces require an exact Bearer authorization header. Missing or incorrect credentials return HTTP `401` with `{"message":"Requires authentication"}` before routing.

## Git smart HTTP and pkt-lines

Git URLs use `/octo-user/{repo}.git`. Discovery is `GET .../info/refs?service=git-upload-pack` or `git-receive-pack`; exchanges are `POST .../git-upload-pack` and `POST .../git-receive-pack`. Responses use `application/x-{service}-advertisement` or `application/x-{service}-result`, HTTP 200, explicit Content-Length, and `Cache-Control: no-cache`.

A pkt-line begins with four ASCII hexadecimal digits describing the total byte length, including that prefix. For example, `0008NAK\n` carries four payload bytes. Special packets are `0000` (flush), `0001` (delimiter), and `0002` (response end). The reader recognizes all three; this server emits flush and delimiter packets. It accepts ordinary lengths from 4 through 65520 and rejects truncated or invalid packets. The writer limits payloads to 65515 bytes.

HTTP discovery starts with a pkt-line containing `# service={service}\n`, then a flush. In v0, ref lines contain `<sha> <ref>\n`, with a NUL followed by space-separated capabilities on the first line. An empty repository advertises the zero SHA and `capabilities^{}`. HEAD is included when the default branch exists.

## Capability advertisement matrix

| Capability / behavior | Upload-pack v0 | Upload-pack v2 | Receive-pack v0 |
| --- | --- | --- | --- |
| Version selection | Default | `Git-Protocol: version=2` during discovery | Always v0, including when version 2 is requested |
| Command advertisement | Ref lines | `version 2`, `ls-refs=unborn`, `fetch=shallow` | Ref lines |
| ACK capabilities | `multi_ack multi_ack_detailed` | Acknowledgments section | — |
| Thin packs | `thin-pack` | Not advertised separately | Parser can resolve bases from existing objects |
| Side bands | `side-band-64k` | Packfile section always banded | `side-band-64k` |
| Offset deltas | `ofs-delta` | Not advertised separately | `ofs-delta` |
| Shallow fetch | `shallow` | `fetch=shallow` | — |
| Object format | Implicit SHA-1 | `object-format=sha1` | Implicit SHA-1 |
| Symbolic HEAD | `symref=HEAD:refs/heads/{default}` if present | `ls-refs` honors `symrefs` | — |
| Push reporting/deletion | — | — | `report-status delete-refs` |
| Agent | `agent=forge/1.0` | `agent=forge/1.0` | `agent=forge/1.0` |

The v2 `ls-refs` handler supports `ref-prefix`, `symrefs`, and `peel`. Although `unborn` is advertised, an empty repository produces an empty ref list rather than an unborn HEAD record. Fetch reads `want`, `have`, `shallow`, `deepen`, and `done`. Without `done`, v2 emits an `acknowledgments` section with ACKs for known haves or NAK. A completed fetch emits optional `shallow-info`, a delimiter, and `packfile` data. Depth must be positive; depth 1 includes tip commits and their trees/blobs, with parent traversal stopped at shallow boundaries. Existing shallow boundaries can receive `unshallow` records.

## Pack object encoding

Packs begin with `PACK`, a big-endian 32-bit version (`2`), and a big-endian 32-bit object count. A final 20-byte SHA-1 covers all preceding pack bytes. The parser verifies this checksum and rejects trailing data.

Each entry starts with a type/size varint. The first byte uses bits 4–6 for type, bits 0–3 for the low size bits, and bit 7 for continuation. Subsequent bytes add seven size bits each, starting at bit 4, with bit 7 again indicating continuation. Types 1–4 mean commit, tree, blob, and tag; 6 means OFS_DELTA and 7 means REF_DELTA. A zlib stream follows the entry header and any delta-base reference. The declared size is the inflated full-object content length, or the delta-program length for a delta entry.

- **OFS_DELTA:** the base is behind the current entry. Decode the first byte as `distance = byte & 0x7f`; for each continuation byte use `distance = ((distance + 1) << 7) | (byte & 0x7f)`. The base offset is `entry_offset - distance`. This represents a negative relative offset using a positive backward distance, not a signed integer. Zero distance and offsets before the pack entries are rejected.
- **REF_DELTA:** the header contains a raw 20-byte base object ID. Bases can occur later in the incoming pack or already exist in the repository, supporting forward references and thin packs. Resolution repeats until all bases are available or fails with `Missing delta base`.
- **Delta program:** base and result lengths are seven-bit continuation varints. An opcode with its high bit set copies from the base; bits 0–3 select little-endian offset bytes and bits 4–6 select length bytes. An omitted/zero copy length means 65536. A nonzero opcode without its high bit inserts that many literal bytes. Zero opcodes, invalid ranges, and result-size mismatches fail validation.

Reconstructed objects are stored as `<type> <size>\0<content>` and addressed by the SHA-1 of those bytes. The writer emits sorted, full objects compressed with zlib; it does not generate delta entries. Limits are 64 MiB per object and 256 MiB for Git request bodies, cumulative inflated pack data, and cumulative reconstructed contents.

## Side-band-64k and push status

The side-band writer splits data into chunks of at most 65514 bytes. Each pkt-line payload starts with channel byte `0x01`, followed by a chunk, so total packet size is at most 65519 bytes. The server emits data on channel 1; it does not emit progress/error channels 2/3. V2 packfile data is always banded. V0 fetches and push status use bands when `side-band-64k` is requested, followed by a flush. Receive-pack also accepts an incoming banded pack containing only channel 1.

Receive-pack reads `<old-sha> <new-sha> <ref>` commands, with NUL-separated capabilities, through a flush, then parses any pack. Pack reconstruction, commit indexing, and object closure validation precede object publication. Ref commands are subsequently applied individually. The status stream starts with `unpack ok\n`, then `ok <ref>\n` or `ng <ref> <reason>\n`, and ends with a flush. A zero new SHA deletes a ref.

The fast-forward enforcement limit is precise: a supplied old SHA different from the current ref produces `ng <ref> non-fast-forward`. A branch target that is not an indexed commit produces `ng <ref> not a commit`. There is no ancestry check when old SHA matches; ref updates are not an atomic multi-ref transaction. Malformed packs produce HTTP 400 JSON errors rather than an `unpack` failure status stream.

## Exact error mapping

REST errors have shape `{"message":"..."}`. Validation order matters when several fields are invalid.

| Condition | HTTP status | Message / behavior |
| --- | --- | --- |
| Foreign owner, absent repository, or unsupported route | 404 | `Not Found`; foreign repositories are hidden for reads and writes. There is no implemented 403 response path. |
| Missing/non-string/empty required text | 422 | `{field} is required` |
| Duplicate label (case-insensitive) | 422 | `label already exists` |
| Label color not exactly six hexadecimal digits | 422 | `Invalid color` |
| Issue labels not a list of strings | 422 | `Invalid labels` |
| Issue references an unknown label | 422 | `Label does not exist` |
| Invalid issue creation body | 422 | `Invalid body` (null is allowed on creation) |
| Invalid issue patch state/title/body | 422 | `Invalid state`, `Invalid title`, or `Invalid body`; patch body/title must be strings |
| Existing file updated/deleted with missing or stale SHA | 409 | `sha does not match the existing file` |
| New file PUT supplies a non-null SHA | 409 | `File does not exist at the supplied sha` |
| DELETE targets missing file | 404 | `Not Found` |
| File/directory collision during PUT | 409 | `Path conflicts with an existing file or directory` |
| Divergent changes to the same merge entry or file/directory merge collision | 405 | `Pull Request is not mergeable` |
| Merge of a closed, unmerged pull request | 409 | `Pull request is closed` |
| Commit listing in empty repository | 409 | `Git Repository is empty` |
| Missing commit/branch/pull | 404 | `No commit found for the ref {ref}`, `Branch not found`, or `Pull request not found` |
| Invalid pagination | 422 | `Invalid pagination` |
| Malformed JSON / non-object REST body | 400 | `Malformed JSON body` / `JSON body must be an object` |
| Oversized JSON body | 413 | `Request body too large` (64 MiB limit) |
| Malformed Git body or pack | 400 | `Invalid Git request: {detail}` |

JSON requests reject Transfer-Encoding and require an unambiguous nonnegative Content-Length. Git requests additionally accept chunked transfer encoding, but reject simultaneous Transfer-Encoding and Content-Length. Other validation errors are detailed in [API.md](API.md).

Authenticated GraphQL execution errors use HTTP 200 and `errors: [{message}]`, with `data: null` on execution failure. MCP maps tool API errors to JSON-RPC `-32602`; unknown methods/tools use `-32601`, invalid requests use `-32600`, and body parsing failures use HTTP 400 with `-32700`. Authentication remains HTTP 401 for both surfaces.
