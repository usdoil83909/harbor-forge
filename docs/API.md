# API reference

All paths below are relative to the printed server URL. Every request requires `Authorization: Bearer <MAKEFUN_SHOP_TOKEN>`. REST lives under `/api/v3`; `{owner}` must be `octo-user`, and repository lookup is case-insensitive. Request and response bodies are JSON unless a response is empty.

## REST routes

In this table, `R` means `/repos/{owner}/{repo}` under `/api/v3`. Status columns list success and route-specific failures. All routes can also return 401 for authentication, 400 for malformed requests, and 500 for unexpected internal errors. JSON bodies over 64 MiB return 413. Missing repositories, foreign owners, unsupported paths, and unsupported method/path combinations return 404, never 403. REST errors are `{message: string}`.

Paginated lists accept `page` (default 1, minimum 1) and `per_page` (default 30, clamped to 1–100); invalid integers return 422. A `Link` header with `rel="next"` is present if another page exists. Lists return arrays rather than an envelope.

| Method | Path | Input / query | Status | Response body |
| --- | --- | --- | --- | --- |
| GET | `/` | — | 200 | `{current_user_url, current_user_repositories_url, repository_url}` |
| GET | `/meta` | — | 200 | `{installed_version:"1.0.0", verifiable_password_authentication:false}` |
| GET | `/user` | — | 200 | `{login:"octo-user", id:1, type:"User"}` |
| GET | `/user/repos` | Pagination | 200, 422 | `Repository[]` |
| POST | `/user/repos` | Required `name`; optional `description`, `auto_init` | 201, 422 | `Repository`; `Location` header |
| GET | `R` | — | 200, 404 | `Repository` |
| PATCH | `R` | Optional `description` string or null | 200, 404, 422 | `Repository` |
| DELETE | `R` | — | 204, 404 | Empty |
| GET | `R/contents` or `R/contents/{path}` | `ref` branch or commit SHA, default `main` | 200, 404, 422 | File with base64 content, or array of immediate file/directory entries |
| PUT | `R/contents/{path}` | Required `message`, base64 `content`; `sha` required for replacement; optional `branch` (default `main`) | 201 new / 200 replacement; 404, 409, 422 | `{content: File, commit: ContentCommit}` |
| DELETE | `R/contents/{path}` | Required `message`, current blob `sha`; optional `branch` | 200, 404, 409, 422 | `{content:null, commit: ContentCommit}` |
| GET | `R/labels` | Pagination | 200, 404, 422 | `Label[]` |
| POST | `R/labels` | Required `name`; optional `color` (default `ededed`), `description` | 201, 404, 422 | `Label` |
| GET | `R/issues` | `state` default `open`, `closed` or `all`; `direction=asc` for ascending number (otherwise descending); pagination | 200, 404, 422 | `Issue[]`, including pull requests |
| POST | `R/issues` | Required `title`; optional `body` string/null, `labels` array of existing names | 201, 404, 422 | `Issue` |
| GET | `R/issues/{number}` | — | 200, 404 | `Issue`, including pull requests |
| PATCH | `R/issues/{number}` | Optional `title`, `body` strings; `state` of `open` or `closed` | 200, 404, 422 | `Issue` |
| GET | `R/pulls` | `state` default `open`, `closed` or `all`; pagination | 200, 404, 422 | `PullRequest[]` in insertion order |
| POST | `R/pulls` | Required `title`, `head`, `base`; optional `body` | 201, 404, 422 | `PullRequest` |
| GET | `R/pulls/{number}` | — | 200, 404 | `PullRequest` |
| GET | `R/pulls/{number}/files` | Pagination | 200, 404, 422 | `[{filename, status:"modified"}]` |
| PUT | `R/pulls/{number}/merge` | Optional `commit_message` string/null | 200, 404, 405, 409, 422 | `{merged:true, sha, message:"Pull Request successfully merged"}` |
| GET | `R/commits` | `sha` branch/commit (default `main`); optional `path`; pagination | 200, 404, 409, 422 | `Commit[]` along first-parent history |

There are no individual label routes or dedicated pull-request PATCH routes. Issue PATCH can update a pull request's title, body, and state. Unrecognized payload fields generally have no effect. Invalid list `state` values simply match no entries.

### REST body shapes and semantics

- **Repository:** `id`, `name`, `full_name`, `owner:{login,id,type}`, `private:false`, `default_branch:"main"`, `description`, `created_at`, `updated_at`, `pushed_at`, plus `url`, `html_url`, `contents_url`, `issues_url`, `labels_url`, `commits_url`, and `clone_url`. Creation accepts names matching `[A-Za-z0-9_.-]+` except `.` and `..`; duplicate names are rejected case-insensitively. Description must be a string or null. Truthy `auto_init` creates a README commit.
- **File:** `name`, `path`, blob `sha`, `size`, `type:"file"`. GET of one file adds `content` and `encoding:"base64"`; directory listings and mutation results omit content. Directory entries have `name`, `path`, tree `sha`, `size:0`, `type:"dir"`. Empty/nonexistent directory listings return 404.
- **Commit:** `{sha, commit:{message, author, committer, tree:{sha}}, parents:[{sha}]}`. Author/committer contain `name`, `email`, and `date`. **ContentCommit** flattens the nested `commit` object into the top level alongside `sha` and `parents`.
- **Label:** `{id,name,color,description,default:false}`. Names are nonempty and unique case-insensitively; color is six hexadecimal digits normalized to lowercase. Description is passed through without type validation.
- **Issue:** `{number,id,title,body,state,labels,created_at,updated_at,closed_at,user:{login},comments:0,url}`. Labels are copied label objects. Creation requires nonempty title, but PATCH permits an empty string. PATCH does not update labels, and requires a string body rather than null.
- **PullRequest:** Issue fields plus `merged`, `mergeable`, `merge_commit_sha`, `head:{ref,sha}`, `base:{ref,sha}`, `merged_at`, `node_id`, and `pull_request:{url}`. A completed merge also stores `merged_commit`. The pulls routes use a pull URL and recompute mergeability and current head/base SHAs for unmerged pulls; issue GET/list uses the issue URL and stored fields.

Content mutations require an existing branch, except the first write to `main` in an empty repository. SHA checks use the **blob** SHA, not the commit SHA. Invalid path components (empty, `.`, `..`, or containing NUL), missing message, invalid branch values, or invalid base64 return 422. Stale SHA and file/directory collisions return 409; see [PROTOCOL.md](PROTOCOL.md) for exact messages.

Pull creation requires distinct existing short branch names. Head may be prefixed with `octo-user:`; another owner or a `refs/` name is rejected with 422. Body is passed through without validation. Merging compares tree entries against a common ancestor, detects conflicting changes and file/directory collisions, and creates a two-parent commit on the base branch. It does not perform line-level text merging. Conflicts return 405, closed unmerged pulls return 409, and repeating an already completed merge returns its existing SHA. Changed-file listings compare the current head snapshot with the base SHA stored at creation and mark every difference `modified`.

## GraphQL

`POST /api/graphql` accepts `{ "query": "...", "variables": {}, "operationName": "..." }`. Only `query` is required. A JSON array batches requests. A document with multiple operations executes all of them unless `operationName` selects one; multiple results form an array. Aliases, named/inline fragments, variable defaults, and `@skip`/`@include` are supported.

Successful operations return `{data: ...}`. Execution failures return `{data:null, errors:[{message}]}`; parsing failures return `{errors:[{message}]}`. These use HTTP 200, including malformed JSON on this endpoint; authentication errors still use HTTP 401.

### Root fields and mutation inputs

| Root field | Arguments / input | Result |
| --- | --- | --- |
| `repository` | `owner`, `name` | Repository or null; owner must be `octo-user` |
| `node` | `id` | Repository, Issue, PullRequest, or null |
| `viewer` | None | User with login and ID |
| `__type` | `name` | Minimal type information or null |
| `__schema` | None | Query/mutation types, types list, empty directives, null subscription type |
| `createPullRequest` | `input:{repositoryId,title,headRefName,baseRefName,body?,clientMutationId?}` | `{pullRequest,clientMutationId}` |
| `mergePullRequest` | `input:{pullRequestId,clientMutationId?}` | `{pullRequest,clientMutationId}` |

Mutation IDs must identify the appropriate existing object. Creation delegates to the same validation as REST. Merge delegates to the same merge implementation; options such as merge method or expected head SHA are not implemented. `clientMutationId` is echoed.

### Object fields

| Type | Supported fields |
| --- | --- |
| Repository | `id databaseId name nameWithOwner owner defaultBranchRef viewerPermission hasIssuesEnabled issue issues pullRequest pullRequests` |
| Issue | `id number title state body createdAt updatedAt url author repository` |
| PullRequest | `id number title body headRefName baseRefName mergeable state merged files createdAt updatedAt url author repository` |
| User | `id login` |
| Ref | `name target` |
| Commit | `id oid` |
| IssueConnection / PullRequestConnection / PullRequestChangedFileConnection | `nodes edges totalCount pageInfo` |
| Edge | `node cursor` |
| PageInfo | `hasNextPage hasPreviousPage startCursor endCursor` |
| PullRequestChangedFile | `path additions deletions changeType` |
| CreatePullRequestPayload / MergePullRequestPayload | `pullRequest clientMutationId` |

`__typename` is available on objects. `issue(number:)` excludes pulls; `pullRequest(number:)` selects pulls. `issues` and `pullRequests` accept `states`, `orderBy`, `first`, and `after`. Sorting uses creation time and number; only `orderBy.direction` affects it, defaulting to descending. States are uppercase (`OPEN`, `CLOSED`, and `MERGED` for merged pulls). Connections use decimal offset cursors; `first` must be nonnegative. Pull `files` also accepts `first` and `after`; additions/deletions are always zero and changeType is always `MODIFIED`.

Repository `viewerPermission` is `ADMIN` and `hasIssuesEnabled` is true. Pull mergeability is `MERGEABLE` or `CONFLICTING`. IDs encode `Type:numeric-id` in base64 for repositories/issues/pulls. Repository `databaseId` is its REST numeric ID; REST pull `node_id` equals its GraphQL ID. Commit IDs encode `Commit:sha`. Node lookup only resolves repositories/issues/pulls.

Introspection exposes the object field names above, scalars `String`, `ID`, `Int`, `Boolean`, and enums `SearchType`, `IssueState`, `PullRequestState`. It is a minimal discovery surface: every object field is described as scalar `String`, arguments are empty, and inputFields are null. It is not a full typed GitHub schema, and advertising SearchType does not provide a search root field.

## MCP JSON-RPC and shared IDs

`POST /mcp` implements JSON responses over Streamable HTTP using JSON-RPC 2.0 envelopes `{jsonrpc:"2.0",id,method,params}`. It supports request arrays (empty arrays return `-32600`). There is no SSE GET stream, session ID, or DELETE session endpoint.

| Method | Parameters | Result |
| --- | --- | --- |
| `initialize` | Optional `protocolVersion` | Requested version echoed, or default `2026-07-28`; `capabilities:{tools:{}}`; `serverInfo:{name:"forge-mcp",version:"1.0.0"}` |
| `notifications/initialized` | None | Empty HTTP 202 for a standalone notification |
| `server/discover` | None | `{supportedVersions:["2026-07-28"]}` |
| `tools/list` | None | `{tools:[{name,description,inputSchema},...]}` |
| `tools/call` | `{name,arguments}` | `{content:[{type:"text",text:"<serialized REST JSON>"}]}` |

Initialization echoes a requested revision without checking it against supportedVersions. Other responses normally use HTTP 200. The implementation only special-cases the standalone initialized notification; batch entries retain their JSON-RPC responses, including null IDs where omitted.

Every tool advertises an object schema. Properties below are strings unless marked integer; optional properties are explicitly marked. Schemas do not set `additionalProperties:false`; the handler extracts recognized arguments.

| Tool name | Argument schema | REST operation |
| --- | --- | --- |
| `create_repo` | Required `name` | POST `/user/repos` |
| `get_repo` | Required `owner`, `repo` | GET `R` |
| `create_issue` | Required `owner`, `repo`, `title`; optional `body` | POST `R/issues` |
| `list_issues` | Required `owner`, `repo`; optional `state` (default `open`) | GET `R/issues` |
| `create_pull_request` | Required `owner`, `repo`, `title`, `head`, `base` | POST `R/pulls` |
| `merge_pull_request` | Required `owner`, `repo`, `pull_number` (integer, not boolean) | PUT `R/pulls/{pull_number}/merge` |

Arguments must have the advertised types; required values cannot be absent, null, or empty. Names are normalized by removing underscores/hyphens and lowercasing; tool matching also permits a prefix before the normalized tool name. An additional string `fullName` containing `owner/repo` can supply missing owner and repo arguments. `list_issues` uses the REST default page size (30); the tool exposes no pagination arguments.

MCP delegates directly to `Handler.dispatch` against the same `Store` and `Repository` instances as REST. A repository or issue created through MCP is immediately readable through REST with the **same numeric `id`**, repository-local issue/pull `number`, and stored fields. There is no MCP-specific ID allocation or copied database. GraphQL mutations use this store too, and its opaque IDs encode the same underlying numeric IDs. Persistence preserves these identities across restarts.

Tool validation and REST API failures return JSON-RPC errors with code `-32602` and the validation/API message. Unknown methods or tools use `-32601`; invalid requests or empty batches use `-32600`; JSON/body parsing failures use HTTP 400 and `-32700`. Errors use `{jsonrpc:"2.0",id,error:{code,message}}` rather than a tool content result.
