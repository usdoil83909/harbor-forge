# Harbor Forge — a GitHub-compatible git forge in one pure-stdlib Python file

[![Built for make.fun](https://img.shields.io/badge/built%20for-make.fun-8B5CF6)](https://make.fun)
[![bounty](https://img.shields.io/badge/bounty-github--core--clone--01-22c55e)](https://make.fun/c/github-core-clone-01)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Harbor Forge implements a focused GitHub-compatible forge in [main.py](main.py), using only Python's standard library. Git transport, REST, GraphQL, and MCP operate on the same repositories, objects, issues, and pull requests. The authenticated user is always `octo-user`.

## What it implements

- **Git smart HTTP:** upload-pack protocol v2 (`ls-refs`, `fetch`) and v0, receive-pack v0, SHA-1 pack parsing and writing, incoming OFS_DELTA and REF_DELTA objects (including thin packs and forward reference deltas), shallow depth-1 clones, ref deletion, and report-status. Outgoing packs contain full objects. Receive-pack enforces the expected old ref SHA and reports stale updates as `non-fast-forward`; it does **not** enforce ancestry-based fast-forward updates when the supplied old SHA matches.
- **REST v3 surface:** repository creation, listing, metadata updates and deletion; file/directory contents and content commits; labels; issues; pull requests, changed files, merges; and commit history.
- **GraphQL v4-style surface:** introspection, repository lookup, issues and pullRequests connections, node lookup, viewer, `createPullRequest`, `mergePullRequest`, variables, aliases, fragments, and batched operations. Introspection is minimal: field types are reported as `String`, and argument/input type metadata is not complete.
- **MCP JSON-RPC:** a JSON POST form of Streamable HTTP at `/mcp`, advertising revision `2026-07-28`, with six tools backed directly by REST dispatch. It has no SSE stream or session lifecycle endpoints.
- **Shared storage:** Git pushes become visible through REST snapshots, and API-created commits can be fetched by Git. Pull requests and issues share repository-local numbering; global numeric IDs come from one store counter.

This is an implemented subset of GitHub's APIs. The exact supported routes, fields, and validation behavior are documented in [docs/API.md](docs/API.md) and [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Quick start

Use Python 3.13 as recommended by the source. The server needs no third-party packages or Git executable; the benchmark and Git client examples require Git.

```sh
export MAKEFUN_ENV_URL="http://127.0.0.1:YOUR_ENV_PORT"
export MAKEFUN_SHOP_TOKEN="your-shop-token"
python3 main.py
```

The server binds to `127.0.0.1` on an automatically selected port, registers that port with `$MAKEFUN_ENV_URL/register`, and prints its base URL. It polls `$MAKEFUN_ENV_URL/done` every approximately five seconds and exits when `done` is `true`. For standalone use, leave `MAKEFUN_ENV_URL` unset. The token defaults to `tok` if unset.

Set `FORGE_URL` to the printed URL, then create and clone a repository:

```sh
export FORGE_URL="http://127.0.0.1:PRINTED_PORT"
curl -H "Authorization: Bearer $MAKEFUN_SHOP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"demo","auto_init":true}' \
  "$FORGE_URL/api/v3/user/repos"
git -c "http.extraHeader=Authorization: Bearer $MAKEFUN_SHOP_TOKEN" \
  -c protocol.version=2 clone --depth 1 "$FORGE_URL/octo-user/demo.git"
```

Every endpoint requires the exact `Authorization: Bearer <token>` header, including Git discovery. State persists to `.local/forge/state.json` by default. Set `MAKEFUN_STATE_DIR` to another directory, or to an empty string for an in-memory store. Snapshots preserve IDs, refs, metadata, and Git objects across restarts.

## Wire-contract summary

| Surface | Endpoint | Contract |
| --- | --- | --- |
| REST | `/api/v3` | JSON objects/arrays; errors `{ "message": "..." }`; paginated lists use `page`, `per_page`, and a next-page `Link` header |
| GraphQL | `POST /api/graphql` | `{query, variables?, operationName?}` or an array; HTTP 200 with `data` and/or `errors` for GraphQL failures |
| MCP | `POST /mcp` | JSON-RPC 2.0 requests/batches; JSON results; initialized notification returns empty HTTP 202 |
| Git discovery | `GET /octo-user/{repo}.git/info/refs?service=git-upload-pack` (or `git-receive-pack`) | Pkt-line advertisement; `Git-Protocol: version=2` selects upload-pack v2 |
| Git fetch | `POST /octo-user/{repo}.git/git-upload-pack` | Pkt-lines, optional shallow information, and a SHA-1 PACK stream |
| Git push | `POST /octo-user/{repo}.git/git-receive-pack` | v0 ref commands plus optional pack; `unpack ok`, per-ref `ok`/`ng` |

## Architecture inside main.py

The implementation is one file with logical modules:

| Area | Main symbols | Responsibility |
| --- | --- | --- |
| Wire and object codecs | `pktline`, `read_pkt`, `sideband`, `apply_delta`, `parse_pack`, `write_pack` | Frame packets, validate pack checksums, inflate objects, reconstruct deltas, emit packs |
| Git graph and transport | `reachable`, `index_commits`, `git_advertisement`, `git_upload_pack`, `git_receive_pack` | Traverse object closures, expose pushed snapshots, negotiate refs/fetches, validate and apply pushes |
| GraphQL | `GraphQLParser`, `GraphQL`, `handle_graphql` | Parse selections and operations, resolve objects, expose introspection and mutations |
| MCP | `MCP_TOOLS`, `handle_mcp` | Describe tools, validate arguments, delegate to REST dispatch |
| Storage | `Repository`, `Store` | Objects, refs, snapshots, labels/issues, IDs, deterministic timestamps, atomic JSON persistence |
| HTTP and REST | `Handler`, `APIError` | Authentication, request framing, routing, validation, content edits, and merges |
| Runtime | `ForgeServer`, `main`, `poll_done` | Threaded serving, environment registration, and completion polling |

`ForgeServer` uses threads and a request backlog of 128. HTTP/1.1 keep-alive, explicit response lengths, and TCP_NODELAY reduce repeated-request latency; connections have a ten-second timeout. Ordinary repository work uses per-repository reentrant locks after releasing the registry lock, allowing work on different repositories to overlap. GraphQL takes the registry and all repository locks for execution. Persistence snapshots also lock all repositories, then write and atomically replace the state file.

Push validation uses a quarantine mapping and remembers validated immutable object closures. Each repository caches one depth-1 pack for requests without haves or shallow boundaries; the cache key includes all refs and wanted IDs, so ref changes invalidate it lazily. Ordinary issue reads serialize a snapshot after releasing the repository lock.

See [BENCHMARK.md](BENCHMARK.md) for measurements and reproduction instructions. The project is licensed under [MIT](LICENSE).
