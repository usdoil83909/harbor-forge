"""A stdlib-only forge. REST, storage, and protocol entry points live here.

Run with Python 3.13. MAKEFUN_STATE_DIR defaults to .local/forge; set it to
an empty string to disable persistence. Stored objects include their Git headers.
"""

import base64
import binascii
import datetime
import hashlib
import hmac
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LOGIN = "octo-user"
EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def git_receive_pack(repo, body):
    raise NotImplementedError("Git receive-pack is reserved for a later session")


def git_upload_pack(repo, body, params):
    raise NotImplementedError("Git upload-pack is reserved for a later session")


def handle_graphql(payload):
    raise NotImplementedError("GraphQL is reserved for a later session")


def handle_mcp(payload):
    raise NotImplementedError("MCP is reserved for a later session")


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class Repository:
    """Git object database, branch refs, REST metadata, and a per-repo lock."""

    def __init__(self, metadata):
        self.lock = threading.Lock()
        self.metadata = metadata
        self.objects = {}
        self.refs = {}
        self.commits = {}
        self.labels = {}
        self.issues = {}

    def object(self, kind, content):
        raw = kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
        sha = hashlib.sha1(raw).hexdigest()
        self.objects[sha] = raw
        return sha

    def blob(self, sha):
        return self.objects[sha].split(b"\0", 1)[1]

    def tree(self, files):
        root = {}
        for path, sha in files.items():
            node = root
            parts = path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = sha

        def build(node):
            entries = []
            for name, value in node.items():
                directory = isinstance(value, dict)
                sha = build(value) if directory else value
                mode = b"40000" if directory else b"100644"
                encoded = name.encode("utf-8")
                entries.append((encoded + (b"/" if directory else b""),
                                mode + b" " + encoded + b"\0" + bytes.fromhex(sha)))
            return self.object("tree", b"".join(v for _, v in sorted(entries)))

        return build(root)

    def resolve(self, ref):
        sha = self.refs.get(ref, ref)
        if sha not in self.commits:
            raise APIError(404, "No commit found for the ref " + ref)
        return sha

    def snapshot(self, branch):
        if branch not in self.refs:
            if not self.refs and branch == "main":
                return {}
            raise APIError(404, "Branch not found")
        return dict(self.commits[self.refs[branch]]["files"])

    def commit(self, files, message, branch, date, changed):
        tree = self.tree(files)
        parent = self.refs.get(branch)
        seconds = int(datetime.datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())
        identity = f"{LOGIN} <{LOGIN}@users.noreply.local> {seconds} +0000"
        content = f"tree {tree}\n"
        if parent:
            content += f"parent {parent}\n"
        content += f"author {identity}\ncommitter {identity}\n\n{message}\n"
        sha = self.object("commit", content.encode())
        self.commits[sha] = {"tree": tree, "parent": parent, "message": message,
                             "date": date, "files": files, "changed": changed}
        self.refs[branch] = sha
        self.metadata["updated_at"] = date
        self.metadata["pushed_at"] = date
        return sha

    def dump(self):
        return {"metadata": self.metadata, "refs": self.refs, "commits": self.commits,
                "labels": self.labels, "issues": self.issues,
                "objects": {sha: base64.b64encode(raw).decode()
                            for sha, raw in self.objects.items()}}

    @classmethod
    def load(cls, data):
        repo = cls(data["metadata"])
        for key in ("refs", "commits", "labels", "issues"):
            setattr(repo, key, data[key])
        repo.objects = {sha: base64.b64decode(raw) for sha, raw in data["objects"].items()}
        return repo


class Store:
    """Lock ordering is always store lock then repository lock.

    The store lock also serializes deterministic counters and atomic persistence.
    All request dispatch and JSON serialization happen while it is held.
    """

    def __init__(self, state_dir):
        self.lock = threading.Lock()
        self.repos = {}
        self.counter = 0
        self.next_id = 1
        self.path = Path(state_dir) / "state.json" if state_dir else None
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text())
            self.counter = data["counter"]
            self.next_id = data["next_id"]
            self.repos = {name: Repository.load(repo) for name, repo in data["repos"].items()}

    def date(self):
        date = EPOCH + datetime.timedelta(seconds=self.counter)
        self.counter += 1
        return date.strftime("%Y-%m-%dT%H:%M:%SZ")

    def identifier(self):
        value = self.next_id
        self.next_id += 1
        return value

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"counter": self.counter, "next_id": self.next_id,
                "repos": {name: repo.dump() for name, repo in self.repos.items()}}
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


def required_text(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise APIError(422, key + " is required")
    return value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_error(self, code, message=None, explain=None):
        # Keep parser errors and unsupported HTTP verbs in the JSON API contract.
        self.close_connection = True
        if hasattr(self, "headers") and not self.authorized():
            code, message = 401, "Requires authentication"
        elif code == 501:
            code, message = 404, "Not Found"
        self.respond(code, {"message": message or "Bad Request"}, {"Connection": "close"})

    def date_time_string(self, timestamp=None):
        # BaseHTTPRequestHandler normally injects wall-clock time into responses.
        return "Thu, 01 Jan 2026 00:00:00 GMT"

    @property
    def store(self):
        return self.server.store

    @property
    def base(self):
        return self.server.base_url + "/api/v3"

    def respond(self, status, payload=None, headers=None):
        body = b"" if status == 204 else json.dumps(
            payload, ensure_ascii=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-GitHub-Request-Id", uuid.uuid4().hex)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_expect_100(self):
        if not self.authorized():
            self.close_connection = True
            self.respond(401, {"message": "Requires authentication"}, {"Connection": "close"})
            return False
        return super().handle_expect_100()

    def authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                   ("Bearer " + self.server.token).encode())

    def read_payload(self):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise APIError(400, "Unsupported Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            self.close_connection = True
            raise APIError(400, "Invalid Content-Length")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError
        except ValueError:
            self.close_connection = True
            raise APIError(400, "Invalid Content-Length")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError(400, "Malformed JSON body")
        if not isinstance(payload, dict):
            raise APIError(400, "JSON body must be an object")
        return payload

    def request(self):
        if not self.authorized():
            self.close_connection = True
            self.respond(401, {"message": "Requires authentication"}, {"Connection": "close"})
            return
        try:
            payload = self.read_payload()
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.rstrip("/").split("/")[1:]]
            query = urllib.parse.parse_qs(parsed.query)
            with self.store.lock:
                status, result, headers = self.dispatch(parts, query, payload)
                if self.command in ("POST", "PUT", "PATCH", "DELETE"):
                    self.store.save()
                self.respond(status, result, headers)
        except APIError as error:
            self.respond(error.status, {"message": error.message})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = request

    def repo_json(self, repo):
        result = dict(repo.metadata)
        url = self.base + "/repos/" + result["full_name"]
        result.update(url=url, html_url=self.server.base_url + "/" + result["full_name"],
                      contents_url=url + "/contents/{+path}", issues_url=url + "/issues{/number}",
                      labels_url=url + "/labels{/name}", commits_url=url + "/commits{/sha}",
                      clone_url=self.server.base_url + "/" + result["full_name"] + ".git")
        return result

    def paginate(self, values, query):
        try:
            size = min(100, max(1, int(query.get("per_page", ["30"])[0])))
            page = max(1, int(query.get("page", ["1"])[0]))
        except ValueError:
            raise APIError(422, "Invalid pagination")
        start = (page - 1) * size
        headers = {}
        if start + size < len(values):
            params = dict(query, per_page=[str(size)], page=[str(page + 1)])
            url = self.server.base_url + urllib.parse.urlsplit(self.path).path
            url += "?" + urllib.parse.urlencode(params, doseq=True)
            headers["Link"] = '<' + url + '>; rel="next"'
        return 200, values[start:start + size], headers

    def dispatch(self, parts, query, payload):
        method = self.command
        if parts[:2] != ["api", "v3"]:
            raise APIError(404, "Not Found")
        route = parts[2:]
        if not route and method == "GET":
            return 200, {"current_user_url": self.base + "/user",
                         "current_user_repositories_url": self.base + "/user/repos",
                         "repository_url": self.base + "/repos/{owner}/{repo}"}, {}
        if route == ["meta"] and method == "GET":
            return 200, {"installed_version": "1.0.0", "verifiable_password_authentication": False}, {}
        if route == ["user"] and method == "GET":
            return 200, {"login": LOGIN, "id": 1, "type": "User"}, {}
        if route == ["user", "repos"]:
            if method == "GET":
                return self.paginate([self.repo_json(repo) for repo in self.store.repos.values()], query)
            if method == "POST":
                name = required_text(payload, "name")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", ".."):
                    raise APIError(422, "Invalid repository name")
                if name.lower() in self.store.repos:
                    raise APIError(422, "name already exists")
                description = payload.get("description", "")
                if description is not None and not isinstance(description, str):
                    raise APIError(422, "Invalid description")
                date = self.store.date()
                repo = Repository({"id": self.store.identifier(), "name": name,
                                   "full_name": LOGIN + "/" + name,
                                   "owner": {"login": LOGIN, "id": 1, "type": "User"},
                                   "private": False, "default_branch": "main",
                                   "description": description, "created_at": date,
                                   "updated_at": date, "pushed_at": None})
                with repo.lock:
                    if payload.get("auto_init"):
                        blob = repo.object("blob", ("# " + name + "\n").encode())
                        repo.commit({"README.md": blob}, "Initial commit", "main", self.store.date(), ["README.md"])
                    self.store.repos[name.lower()] = repo
                return 201, self.repo_json(repo), {"Location": self.base + "/repos/" + LOGIN + "/" + name}
        if len(route) < 3 or route[0] != "repos" or route[1] != LOGIN:
            raise APIError(404, "Not Found")
        repo = self.store.repos.get(route[2].lower())
        if repo is None:
            raise APIError(404, "Not Found")
        with repo.lock:
            return self.repository_route(repo, route[3:], query, payload)

    def repository_route(self, repo, route, query, payload):
        method = self.command
        if not route:
            if method == "GET":
                return 200, self.repo_json(repo), {}
            if method == "PATCH":
                if "description" in payload:
                    if payload["description"] is not None and not isinstance(payload["description"], str):
                        raise APIError(422, "Invalid description")
                    repo.metadata["description"] = payload["description"]
                repo.metadata["updated_at"] = self.store.date()
                return 200, self.repo_json(repo), {}
            if method == "DELETE":
                del self.store.repos[repo.metadata["name"].lower()]
                return 204, None, {}
        if route and route[0] == "contents":
            return self.contents(repo, "/".join(route[1:]), query, payload)
        if route == ["labels"]:
            if method == "GET":
                return self.paginate(list(repo.labels.values()), query)
            if method == "POST":
                name = required_text(payload, "name")
                if name.lower() in repo.labels:
                    raise APIError(422, "label already exists")
                color = payload.get("color", "ededed")
                if not isinstance(color, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", color):
                    raise APIError(422, "Invalid color")
                label = {"id": self.store.identifier(), "name": name, "color": color.lower(),
                         "description": payload.get("description"), "default": False}
                repo.labels[name.lower()] = label
                return 201, label, {}
        if route == ["issues"]:
            if method == "POST":
                title = required_text(payload, "title")
                names = payload.get("labels", [])
                if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
                    raise APIError(422, "Invalid labels")
                if any(n.lower() not in repo.labels for n in names):
                    raise APIError(422, "Label does not exist")
                body = payload.get("body")
                if body is not None and not isinstance(body, str):
                    raise APIError(422, "Invalid body")
                number = len(repo.issues) + 1
                date = self.store.date()
                issue = {"number": number, "id": self.store.identifier(), "title": title,
                         "body": body, "state": "open", "labels": [dict(repo.labels[n.lower()]) for n in names],
                         "created_at": date, "updated_at": date, "closed_at": None,
                         "user": {"login": LOGIN}, "comments": 0,
                         "url": self.base + "/repos/" + repo.metadata["full_name"] + "/issues/" + str(number)}
                repo.issues[str(number)] = issue
                return 201, issue, {}
            if method == "GET":
                state = query.get("state", ["open"])[0]
                issues = [i for i in repo.issues.values() if state == "all" or i["state"] == state]
                issues.sort(key=lambda i: i["number"], reverse=query.get("direction", ["desc"])[0] != "asc")
                return self.paginate([self.issue_json(repo, i) for i in issues], query)
        if len(route) == 2 and route[0] == "issues" and method == "GET":
            issue = repo.issues.get(route[1])
            if issue is None:
                raise APIError(404, "Not Found")
            return 200, self.issue_json(repo, issue), {}
        if route == ["commits"] and method == "GET":
            if not repo.refs:
                raise APIError(409, "Git Repository is empty")
            sha = repo.resolve(query.get("sha", ["main"])[0])
            path = query.get("path", [""])[0].strip("/")
            commits = []
            while sha:
                commit = repo.commits[sha]
                if not path or any(p == path or p.startswith(path + "/") for p in commit["changed"]):
                    commits.append(self.commit_json(repo, sha))
                sha = commit["parent"]
            return self.paginate(commits, query)
        raise APIError(404, "Not Found")

    def issue_json(self, repo, issue):
        result = dict(issue)
        result["url"] = self.base + "/repos/" + repo.metadata["full_name"] + "/issues/" + str(issue["number"])
        return result

    def commit_json(self, repo, sha):
        commit = repo.commits[sha]
        person = {"name": LOGIN, "email": LOGIN + "@users.noreply.local", "date": commit["date"]}
        return {"sha": sha, "commit": {"message": commit["message"], "author": person,
                                      "committer": dict(person), "tree": {"sha": commit["tree"]}},
                "parents": [{"sha": commit["parent"]}] if commit["parent"] else []}

    def file_json(self, repo, path, sha, include_content=True):
        raw = repo.blob(sha)
        result = {"name": path.rsplit("/", 1)[-1], "path": path, "sha": sha,
                  "size": len(raw), "type": "file"}
        if include_content:
            result.update(content=base64.b64encode(raw).decode(), encoding="base64")
        return result

    def contents(self, repo, path, query, payload):
        method = self.command
        if method not in ("GET", "PUT", "DELETE"):
            raise APIError(404, "Not Found")
        if path and any(p in ("", ".", "..") or "\0" in p for p in path.split("/")):
            raise APIError(422, "Invalid path")
        if method == "GET":
            ref = query.get("ref", ["main"])[0]
            if not repo.refs:
                raise APIError(404, "Not Found")
            files = repo.commits[repo.resolve(ref)]["files"]
            if path in files:
                return 200, self.file_json(repo, path, files[path]), {}
            prefix = path + "/" if path else ""
            entries = {}
            for name, sha in sorted(files.items()):
                if not name.startswith(prefix):
                    continue
                rest = name[len(prefix):]
                if "/" in rest:
                    directory = prefix + rest.split("/", 1)[0]
                    subtree = {p[len(directory) + 1:]: s for p, s in files.items() if p.startswith(directory + "/")}
                    entries[directory] = {"name": rest.split("/", 1)[0], "path": directory,
                                          "type": "dir", "sha": repo.tree(subtree), "size": 0}
                else:
                    entries[name] = self.file_json(repo, name, sha, False)
            if not entries:
                raise APIError(404, "Not Found")
            return 200, list(entries.values()), {}
        if not path:
            raise APIError(422, "Path is required")
        branch = payload.get("branch", "main")
        if not isinstance(branch, str) or not branch:
            raise APIError(422, "Invalid branch")
        files = repo.snapshot(branch)
        existing = files.get(path)
        if method == "DELETE" and existing is None:
            raise APIError(404, "Not Found")
        if existing is not None and payload.get("sha") != existing:
            raise APIError(409, "sha does not match the existing file")
        if existing is None and payload.get("sha") is not None:
            raise APIError(409, "File does not exist at the supplied sha")
        message = required_text(payload, "message")
        if method == "PUT":
            encoded = payload.get("content")
            if not isinstance(encoded, str):
                raise APIError(422, "content must be base64 encoded")
            try:
                raw = base64.b64decode("".join(encoded.split()), validate=True)
            except (ValueError, binascii.Error):
                raise APIError(422, "Invalid base64 content")
            if any(p.startswith(path + "/") or path.startswith(p + "/") for p in files):
                raise APIError(409, "Path conflicts with an existing file or directory")
            files[path] = repo.object("blob", raw)
        else:
            del files[path]
        sha = repo.commit(files, message, branch, self.store.date(), [path])
        content = self.file_json(repo, path, files[path], False) if method == "PUT" else None
        status = 201 if method == "PUT" and existing is None else 200
        commit = self.commit_json(repo, sha)
        commit.update(commit.pop("commit"))
        return status, {"content": content, "commit": commit}, {}


def poll_done(environment_url):
    url = environment_url.rstrip("/") + "/done"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=65) as response:
                if json.load(response).get("done") is True:
                    os._exit(0)
        except (OSError, ValueError, urllib.error.URLError):
            pass
        threading.Event().wait(0.25)


def main():
    store = Store(os.environ.get("MAKEFUN_STATE_DIR", ".local/forge"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.store = store
    server.token = os.environ.get("MAKEFUN_SHOP_TOKEN", "tok")
    port = server.server_address[1]
    server.base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.get("MAKEFUN_ENV_URL")
    if environment:
        request = urllib.request.Request(environment.rstrip("/") + "/register",
                                         data=json.dumps({"port": port}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        threading.Thread(target=poll_done, args=(environment,), daemon=True).start()
    print(server.base_url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
