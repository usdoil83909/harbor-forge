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
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LOGIN = "octo-user"
EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


ZERO = "0" * 40
OBJECT_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
MAX_GIT_BODY = 256 * 1024 * 1024
MAX_OBJECT = 64 * 1024 * 1024


def pktline(data):
    if isinstance(data, str):
        data = data.encode()
    if len(data) > 65515:
        raise ValueError("pkt-line too large")
    return f"{len(data) + 4:04x}".encode() + data


def read_pkt(data, offset):
    if offset + 4 > len(data):
        raise ValueError("Truncated pkt-line")
    size = int(data[offset:offset + 4], 16)
    if size in (0, 1, 2):
        return size, offset + 4
    if size < 4 or size > 65520 or offset + size > len(data):
        raise ValueError("Invalid pkt-line length")
    return data[offset + 4:offset + size], offset + size


def packet_lines(data):
    offset = 0
    while offset < len(data):
        line, offset = read_pkt(data, offset)
        if isinstance(line, bytes):
            yield line.rstrip(b"\n")


def sideband(data):
    return b"".join(pktline(b"\x01" + data[i:i + 65514])
                    for i in range(0, len(data), 65514))


def split_object(raw):
    header, content = raw.split(b"\0", 1)
    return header.split(b" ", 1)[0].decode(), content


def object_raw(kind, content):
    return kind.encode() + b" " + str(len(content)).encode() + b"\0" + content


def apply_delta(base, delta):
    pos = 0

    def integer():
        nonlocal pos
        result = shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            result |= (byte & 127) << shift
            if not byte & 128:
                return result
            shift += 7
            if shift > 63:
                raise ValueError("Invalid delta integer")

    if integer() != len(base):
        raise ValueError("Delta base size mismatch")
    size = integer()
    if size > MAX_OBJECT:
        raise ValueError("Delta object too large")
    result = bytearray()
    while pos < len(delta):
        opcode = delta[pos]
        pos += 1
        if opcode & 128:
            start = length = 0
            for i in range(4):
                if opcode & (1 << i):
                    start |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if opcode & (16 << i):
                    length |= delta[pos] << (8 * i)
                    pos += 1
            length = length or 65536
            if start + length > len(base) or len(result) + length > size:
                raise ValueError("Invalid delta copy")
            result.extend(memoryview(base)[start:start + length])
        elif opcode:
            if pos + opcode > len(delta) or len(result) + opcode > size:
                raise ValueError("Invalid delta insert")
            result.extend(delta[pos:pos + opcode])
            pos += opcode
        else:
            raise ValueError("Invalid delta opcode")
    if len(result) != size:
        raise ValueError("Delta result size mismatch")
    return bytes(result)


def parse_pack(data, existing):
    """Validate into a quarantine dict; resolve forward REF and thin-pack deltas."""
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("Invalid PACK header")
    version, count = struct.unpack_from(">II", data, 4)
    if version != 2 or count > len(data) // 3:
        raise ValueError("Invalid PACK version or count")
    if hashlib.sha1(memoryview(data)[:-20]).digest() != data[-20:]:
        raise ValueError("PACK checksum mismatch")
    end = len(data) - 20
    pos = 12
    offsets, objects, pending = {}, {}, []
    expanded = inflated = 0

    def store(offset, kind, content):
        nonlocal expanded
        expanded += len(content)
        if expanded > MAX_GIT_BODY:
            raise ValueError("Unpacked data too large")
        raw = object_raw(kind, content)
        sha = hashlib.sha1(raw).hexdigest()
        objects[sha] = raw
        offsets[offset] = sha

    for _ in range(count):
        offset = pos
        byte = data[pos]
        pos += 1
        kind, size, shift = (byte >> 4) & 7, byte & 15, 4
        while byte & 128:
            byte = data[pos]
            pos += 1
            size |= (byte & 127) << shift
            shift += 7
            if shift > 63:
                raise ValueError("Invalid object size")
        if size > MAX_OBJECT:
            raise ValueError("Object too large")
        inflated += size
        if inflated > MAX_GIT_BODY:
            raise ValueError("Inflated pack too large")
        base = None
        if kind == 6:
            byte = data[pos]
            pos += 1
            distance = byte & 127
            while byte & 128:
                byte = data[pos]
                pos += 1
                distance = ((distance + 1) << 7) | (byte & 127)
                if distance > offset:
                    raise ValueError("Invalid delta offset")
            base = offset - distance
            if distance == 0 or base < 12:
                raise ValueError("Invalid delta offset")
        elif kind == 7:
            base = data[pos:pos + 20].hex()
            pos += 20
        elif kind not in OBJECT_TYPES:
            raise ValueError("Unsupported object type")
        inflater = zlib.decompressobj()
        content = bytearray()
        while not inflater.eof and pos < end:
            chunk = memoryview(data)[pos:min(end, pos + 65536)]
            out = inflater.decompress(chunk, size + 1 - len(content))
            content.extend(out)
            consumed = len(chunk) - len(inflater.unused_data) - len(inflater.unconsumed_tail)
            pos += consumed
            if len(content) > size or (not consumed and not out):
                raise ValueError("Invalid compressed object size")
        if not inflater.eof or len(content) != size:
            raise ValueError("Truncated compressed object")
        if kind in (6, 7):
            pending.append((offset, base, bytes(content)))
        else:
            store(offset, OBJECT_TYPES[kind], bytes(content))
    if pos != end:
        raise ValueError("Trailing PACK data")
    while pending:
        remaining = []
        for offset, base, delta in pending:
            sha = offsets.get(base) if isinstance(base, int) else base
            raw = objects.get(sha) or existing.get(sha)
            if raw is None:
                remaining.append((offset, base, delta))
                continue
            kind, content = split_object(raw)
            store(offset, kind, apply_delta(content, delta))
        if len(remaining) == len(pending):
            raise ValueError("Missing delta base")
        pending = remaining
    return objects


def write_pack(repo, shas):
    pack = bytearray(b"PACK" + struct.pack(">II", 2, len(shas)))
    types = {name: number for number, name in OBJECT_TYPES.items()}
    for sha in sorted(shas):
        kind, content = split_object(repo.objects[sha])
        size = len(content)
        byte = (types[kind] << 4) | (size & 15)
        size >>= 4
        pack.append(byte | (128 if size else 0))
        while size:
            byte = size & 127
            size >>= 7
            pack.append(byte | (128 if size else 0))
        pack.extend(zlib.compress(content))
    pack.extend(hashlib.sha1(pack).digest())
    return bytes(pack)


def tree_entries(content):
    pos = 0
    while pos < len(content):
        stop = content.index(b"\0", pos)
        mode, name = content[pos:stop].split(b" ", 1)
        if stop + 21 > len(content):
            raise ValueError("Truncated tree entry")
        yield mode, name.decode("utf-8", "surrogateescape"), content[stop + 1:stop + 21].hex()
        pos = stop + 21


def commit_headers(content):
    headers = content.split(b"\n\n", 1)[0].splitlines()
    tree = next(line[5:].decode() for line in headers if line.startswith(b"tree "))
    parents = [line[7:].decode() for line in headers if line.startswith(b"parent ")]
    return tree, parents


def reachable(repo, starts, depth=None, shallow=()):
    seen, boundaries, levels = set(), set(), {}
    queue = [(sha, 1) for sha in starts]
    while queue:
        sha, level = queue.pop()
        if sha in levels and levels[sha] <= level:
            continue
        levels[sha] = level
        raw = repo.objects.get(sha)
        if raw is None:
            raise ValueError("Missing object " + sha)
        seen.add(sha)
        kind, content = split_object(raw)
        if kind == "commit":
            tree, parents = commit_headers(content)
            queue.append((tree, level))
            if (depth is not None and level >= depth) or sha in shallow:
                if parents:
                    boundaries.add(sha)
            else:
                queue.extend((parent, level + 1) for parent in parents)
        elif kind == "tree":
            queue.extend((child, level) for mode, _, child in tree_entries(content)
                         if mode != b"160000")
        elif kind == "tag":
            queue.append((content.splitlines()[0][7:].decode(), level))
    return seen, boundaries


def index_commits(repo, objects):
    """Expose pushed commits through the same snapshot index used by REST."""
    indexed = {}
    for sha, raw in objects.items():
        kind, content = split_object(raw)
        if kind != "commit":
            continue
        tree, parents = commit_headers(content)
        files, stack = {}, [(tree, "")]
        while stack:
            tree_sha, prefix = stack.pop()
            tree_kind, entries = split_object(repo.objects[tree_sha])
            if tree_kind != "tree":
                raise ValueError("Commit references a non-tree")
            for mode, name, child in tree_entries(entries):
                if mode in (b"40000", b"040000"):
                    stack.append((child, prefix + name + "/"))
                elif mode != b"160000":
                    files[prefix + name] = child
        date = "2026-01-01T00:00:00Z"
        for line in content.split(b"\n\n", 1)[0].splitlines():
            if line.startswith(b"committer "):
                seconds = int(line.rsplit(b" ", 2)[1])
                date = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        indexed[sha] = {"tree": tree, "parent": parents[0] if parents else None,
                        "message": content.split(b"\n\n", 1)[-1].decode("utf-8", "replace").rstrip("\n"),
                        "date": date, "files": files, "changed": []}
    for commit in indexed.values():
        parent = indexed.get(commit["parent"]) or repo.commits.get(commit["parent"], {})
        previous = parent.get("files", {})
        commit["changed"] = sorted(path for path in previous.keys() | commit["files"].keys()
                                   if previous.get(path) != commit["files"].get(path))
    return indexed


def full_refs(repo):
    return {name if name.startswith("refs/") else "refs/heads/" + name: sha
            for name, sha in repo.refs.items()}


def git_advertisement(repo, service, v2=False):
    intro = pktline(f"# service={service}\n") + b"0000"
    if v2 and service == "git-upload-pack":
        return intro + b"".join(pktline(line + "\n") for line in
                                ("version 2", "ls-refs=unborn", "fetch=shallow",
                                 "object-format=sha1", "agent=forge/1.0")) + b"0000"
    refs = full_refs(repo)
    head = "refs/heads/" + repo.metadata["default_branch"]
    if service == "git-upload-pack":
        caps = "multi_ack multi_ack_detailed thin-pack side-band-64k ofs-delta shallow agent=forge/1.0"
        if head in refs:
            caps += " symref=HEAD:" + head
    else:
        caps = "report-status delete-refs side-band-64k ofs-delta agent=forge/1.0"
    lines = ([("HEAD", refs[head])] if head in refs else []) + sorted(refs.items())
    if not lines:
        lines = [("capabilities^{}", ZERO)]
    return intro + b"".join(pktline(f"{sha} {ref}" + ("\0" + caps if i == 0 else "") + "\n")
                             for i, (ref, sha) in enumerate(lines)) + b"0000"


def git_receive_pack(repo, body):
    commands, caps, pos = [], set(), 0
    while True:
        line, pos = read_pkt(body, pos)
        if line == 0:
            break
        if not isinstance(line, bytes):
            raise ValueError("Invalid receive command")
        command, separator, capabilities = line.rstrip(b"\n").partition(b"\0")
        if separator:
            caps.update(capabilities.decode().split())
        old, new, ref = command.decode().split()
        if not re.fullmatch(r"[0-9a-f]{40}", old) or not re.fullmatch(r"[0-9a-f]{40}", new):
            raise ValueError("Invalid object ID")
        if not ref.startswith("refs/") or any(c in ref for c in ("..", " ", "~", "^", ":", "?", "*", "[", "\\")):
            raise ValueError("Invalid ref name")
        commands.append((old, new, ref))
    pack = body[pos:]
    if pack and not pack.startswith(b"PACK") and "side-band-64k" in caps:
        chunks = []
        offset = 0
        while offset < len(pack):
            line, offset = read_pkt(pack, offset)
            if line == 0:
                continue
            if not isinstance(line, bytes):
                raise ValueError("Invalid incoming pack packet")
            if line[:1] != b"\x01":
                raise ValueError("Invalid incoming pack band")
            chunks.append(line[1:])
        pack = b"".join(chunks)
    objects = parse_pack(pack, repo.objects) if pack else {}
    # Keep validation and indexing transactional, including malformed trees/commits.
    view = Repository(repo.metadata)
    view.objects = repo.objects | objects
    view.commits = repo.commits
    indexed = index_commits(view, objects)
    for _, new, _ in commands:
        if new != ZERO:
            reachable(view, [new])
    repo.objects.update(objects)
    repo.commits.update(indexed)
    result = pktline("unpack ok\n")
    for old, new, ref in commands:
        key = ref.removeprefix("refs/heads/")
        current = repo.refs.get(key, ZERO)
        if old != current and old != ZERO:
            result += pktline(f"ng {ref} non-fast-forward\n")
        elif new != ZERO and ref.startswith("refs/heads/") and new not in repo.commits:
            result += pktline(f"ng {ref} not a commit\n")
        else:
            if new == ZERO:
                repo.refs.pop(key, None)
            else:
                repo.refs[key] = new
            result += pktline(f"ok {ref}\n")
    result += b"0000"
    return sideband(result) + b"0000" if "side-band-64k" in caps else result


def git_upload_pack(repo, body, params):
    lines = [line.decode() for line in packet_lines(body)]
    command = next((line[8:] for line in lines if line.startswith("command=")), None)
    if command == "ls-refs":
        refs = full_refs(repo)
        head = "refs/heads/" + repo.metadata["default_branch"]
        if head in refs:
            refs = {"HEAD": refs[head], **refs}
        prefixes = [line[11:] for line in lines if line.startswith("ref-prefix ")]
        result = bytearray()
        for ref, sha in refs.items():
            if prefixes and not any(ref.startswith(prefix) for prefix in prefixes):
                continue
            attributes = " symref-target:" + head if ref == "HEAD" and "symrefs" in lines else ""
            if "peel" in lines:
                peeled = sha
                while split_object(repo.objects[peeled])[0] == "tag":
                    peeled = split_object(repo.objects[peeled])[1].splitlines()[0][7:].decode()
                if peeled != sha:
                    attributes += " peeled:" + peeled
            result.extend(pktline(f"{sha} {ref}{attributes}\n"))
        return bytes(result) + b"0000"
    if command not in (None, "fetch"):
        raise ValueError("Unsupported v2 command")
    wants = [line.split()[1] for line in lines if line.startswith("want ")]
    haves = [line.split()[1] for line in lines if line.startswith("have ")]
    shallow = {line.split()[1] for line in lines if line.startswith("shallow ")}
    depth = next((int(line.split()[1]) for line in lines if line.startswith("deepen ")), None)
    if depth is not None and depth < 1:
        raise ValueError("Invalid depth")
    done = "done" in lines
    common = [sha for sha in haves if sha in repo.objects]
    # Protocol v2 uses ACK <oid>/NAK and delimiter packets between sections.
    if command and not done:
        return pktline("acknowledgments\n") + (b"".join(pktline(f"ACK {sha}\n") for sha in common)
                                                 or pktline("NAK\n")) + b"0000"
    wanted, boundaries = reachable(repo, wants, depth)
    have_objects, _ = reachable(repo, common, shallow=shallow)
    pack = write_pack(repo, wanted - have_objects)
    shallow_lines = b""
    if depth is not None:
        shallow_lines = b"".join(pktline(f"shallow {sha}\n") for sha in sorted(boundaries - shallow))
        shallow_lines += b"".join(pktline(f"unshallow {sha}\n") for sha in sorted((shallow & wanted) - boundaries))
    if command:
        result = (pktline("shallow-info\n") + shallow_lines + b"0001") if shallow_lines else b""
        # v2 packfile data is always sideband framed, independent of capabilities.
        return result + pktline("packfile\n") + sideband(pack) + b"0000"
    caps = set(" ".join(line for line in lines if line.startswith("want ")).split())
    result = (shallow_lines + b"0000") if depth is not None else b""
    if depth is not None and not done and not haves:
        return result
    if not done and "multi_ack_detailed" in caps:
        result += b"".join(pktline(f"ACK {sha} common\n") for sha in common)
    result += pktline("NAK\n")
    if not done and haves:
        return result
    return result + (sideband(pack) + b"0000" if "side-band-64k" in caps else pack)


class GraphQLParser:
    """Small recursive parser; selections retain aliases, arguments and fragments."""

    def __init__(self, source, variables):
        self.tokens = []
        pattern = re.compile(r'\s+|#[^\n]*|,|\.\.\.|"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z_0-9]*|[!$():=@\[\]{}]')
        pos = 0
        while pos < len(source):
            match = pattern.match(source, pos)
            if not match:
                raise ValueError("Invalid query at position " + str(pos))
            token = match.group()
            if not token.isspace() and not token.startswith('#') and token != ',':
                self.tokens.append(token)
            pos = match.end()
        self.pos = 0
        self.variables = dict(variables)
        self.fragments = {}

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError("Expected " + str(expected or "token"))
        self.pos += 1
        return token

    def value(self):
        token = self.take()
        if token == '$':
            return self.variables.get(self.take())
        if token == '{':
            result = {}
            while self.peek() != '}':
                key = self.take()
                self.take(':')
                result[key] = self.value()
            self.take('}')
            return result
        if token == '[':
            result = []
            while self.peek() != ']':
                result.append(self.value())
            self.take(']')
            return result
        if token.startswith('"') or token in ('true', 'false', 'null') or re.match(r'-?\d', token):
            return json.loads(token)
        return token

    def arguments(self):
        result = {}
        if self.peek() == '(':
            self.take('(')
            while self.peek() != ')':
                key = self.take()
                self.take(':')
                result[key] = self.value()
            self.take(')')
        return result

    def selection(self):
        self.take('{')
        fields = []
        while self.peek() != '}':
            name = self.take()
            alias, condition = name, None
            if name == '...':
                name = self.take()
                if name == 'on':
                    condition = self.take()
                    name = '...'
                else:
                    name = '...' + name
            elif self.peek() == ':':
                self.take(':')
                name = self.take()
            args = self.arguments()
            include = True
            while self.peek() == '@':
                self.take('@')
                directive = self.take()
                test = self.arguments().get('if')
                if (directive == 'skip' and test) or (directive == 'include' and not test):
                    include = False
            children = self.selection() if self.peek() == '{' else []
            if include:
                fields.append((alias, name, args, children, condition))
        self.take('}')
        return fields

    def document(self):
        operations = []
        while self.peek() is not None:
            if self.peek() == 'fragment':
                self.take()
                name = self.take()
                self.take('on')
                condition = self.take()
                self.fragments[name] = (condition, self.selection())
                continue
            kind, name = 'query', None
            if self.peek() != '{':
                kind = self.take()
                if kind not in ('query', 'mutation'):
                    raise ValueError('Unknown operation ' + kind)
                if self.peek() not in ('(', '{'):
                    name = self.take()
                if self.peek() == '(':
                    self.take('(')
                    while self.peek() != ')':
                        self.take('$')
                        key = self.take()
                        self.take(':')
                        while self.peek() not in ('=', '$', ')'):
                            self.take()
                        if self.peek() == '=':
                            self.take('=')
                            default = self.value()
                            self.variables.setdefault(key, default)
                    self.take(')')
            operations.append((name, kind, self.selection()))
        return operations


GRAPHQL_FIELDS = {
    'Query': 'repository node viewer __type __schema',
    'Mutation': 'createPullRequest mergePullRequest',
    'Repository': 'id databaseId name nameWithOwner owner defaultBranchRef viewerPermission hasIssuesEnabled issue issues pullRequests pullRequest',
    'Issue': 'id number title state body createdAt updatedAt url author repository',
    'PullRequest': 'id number title body headRefName baseRefName mergeable state merged files createdAt updatedAt url author repository',
    'User': 'id login', 'Ref': 'name target', 'Commit': 'id oid',
    'IssueConnection': 'nodes edges totalCount pageInfo',
    'PullRequestConnection': 'nodes edges totalCount pageInfo',
    'PullRequestChangedFileConnection': 'nodes edges totalCount pageInfo',
    'PullRequestChangedFile': 'path additions deletions changeType',
    'Edge': 'node cursor', 'PageInfo': 'hasNextPage hasPreviousPage startCursor endCursor',
    'CreatePullRequestPayload': 'pullRequest clientMutationId',
    'MergePullRequestPayload': 'pullRequest clientMutationId',
}


def gql_object(typename, /, **values):
    return dict(values, __typename=typename)


def gql_id(kind, number):
    return base64.b64encode(f'{kind}:{number}'.encode()).decode()


class GraphQL:
    def __init__(self, handler, parser):
        self.handler, self.parser = handler, parser

    def repository(self, repo):
        return gql_object('Repository', _repo=repo)

    def issue(self, repo, issue):
        return gql_object('PullRequest' if 'head' in issue else 'Issue', _repo=repo, _issue=issue)

    def node(self, identifier):
        for repo in self.handler.store.repos.values():
            if identifier == gql_id('Repository', repo.metadata['id']):
                return self.repository(repo)
            for issue in repo.issues.values():
                kind = 'PullRequest' if 'head' in issue else 'Issue'
                if identifier == gql_id(kind, issue['id']):
                    return self.issue(repo, issue)
        return None

    def connection(self, kind, values, args):
        total = len(values)
        start = int(args.get('after') or 0)
        first = int(args.get('first', total))
        if first < 0:
            raise ValueError('first must be nonnegative')
        nodes = values[start:start + first]
        cursors = [str(start + i + 1) for i in range(len(nodes))]
        return gql_object(kind, nodes=nodes, totalCount=total,
                          edges=[gql_object('Edge', node=n, cursor=c) for n, c in zip(nodes, cursors)],
                          pageInfo=gql_object('PageInfo', hasNextPage=start + len(nodes) < total,
                                              hasPreviousPage=start > 0, startCursor=cursors[0] if cursors else None,
                                              endCursor=cursors[-1] if cursors else None))

    def type_info(self, name):
        enums = {'SearchType': ['ISSUE', 'REPOSITORY', 'USER'], 'IssueState': ['OPEN', 'CLOSED'],
                 'PullRequestState': ['OPEN', 'CLOSED', 'MERGED']}
        if name not in GRAPHQL_FIELDS and name not in enums and name not in ('String', 'ID', 'Int', 'Boolean'):
            return None
        return gql_object('__Type', name=name, kind='ENUM' if name in enums else 'OBJECT' if name in GRAPHQL_FIELDS else 'SCALAR',
                          description=None, ofType=None, interfaces=[], possibleTypes=None, inputFields=None,
                          fields=[gql_object('__Field', name=f, description=None, args=[], isDeprecated=False,
                                             deprecationReason=None, type=gql_object('__Type', kind='SCALAR', name='String', ofType=None))
                                  for f in GRAPHQL_FIELDS.get(name, '').split()] if name in GRAPHQL_FIELDS else None,
                          enumValues=[gql_object('__EnumValue', name=v, description=None, isDeprecated=False,
                                                 deprecationReason=None) for v in enums[name]] if name in enums else None)

    def resolve(self, obj, name, args):
        kind = obj['__typename']
        if name == '__typename':
            return kind
        if kind in GRAPHQL_FIELDS and name not in GRAPHQL_FIELDS[kind].split():
            raise ValueError(f'Unknown field "{name}" on type "{kind}"')
        if kind == 'Query':
            if name == '__type':
                return self.type_info(args.get('name'))
            if name == '__schema':
                return gql_object('__Schema', queryType=self.type_info('Query'), mutationType=self.type_info('Mutation'),
                                  subscriptionType=None, directives=[], types=[self.type_info(n) for n in
                                  list(GRAPHQL_FIELDS) + ['SearchType', 'IssueState', 'PullRequestState', 'String', 'ID', 'Int', 'Boolean']])
            if name == 'viewer':
                return gql_object('User', login=LOGIN, id=gql_id('User', 1))
            if name == 'node':
                return self.node(args.get('id'))
            repo = self.handler.store.repos.get(str(args.get('name', '')).lower()) if args.get('owner') == LOGIN else None
            return self.repository(repo) if repo else None
        if kind == 'Mutation':
            data = args.get('input', {})
            if name == 'createPullRequest':
                node = self.node(data.get('repositoryId'))
                if not node or node['__typename'] != 'Repository':
                    raise ValueError('Unknown repositoryId')
                repo = node['_repo']
                issue = self.handler.create_pull(repo, dict(title=data.get('title'), head=data.get('headRefName'),
                                                            base=data.get('baseRefName'), body=data.get('body')))
                payload_kind = 'CreatePullRequestPayload'
            else:
                node = self.node(data.get('pullRequestId'))
                if not node or node['__typename'] != 'PullRequest':
                    raise ValueError('Unknown pullRequestId')
                repo, issue = node['_repo'], node['_issue']
                self.handler.merge_pull(repo, issue)
                payload_kind = 'MergePullRequestPayload'
            return gql_object(payload_kind, pullRequest=self.issue(repo, issue), clientMutationId=data.get('clientMutationId'))
        if kind == 'Repository':
            repo = obj['_repo']
            meta = repo.metadata
            values = dict(id=gql_id(kind, meta['id']), databaseId=meta['id'], name=meta['name'],
                          nameWithOwner=meta['full_name'], owner=gql_object('User', login=LOGIN, id=gql_id('User', 1)),
                          viewerPermission='ADMIN', hasIssuesEnabled=True,
                          defaultBranchRef=gql_object('Ref', name=meta['default_branch'],
                              target=gql_object('Commit', oid=repo.refs.get(meta['default_branch']),
                                                id=gql_id('Commit', repo.refs.get(meta['default_branch'])))) if repo.refs else None)
            if name in values:
                return values[name]
            pulls = name in ('pullRequest', 'pullRequests')
            if name in ('issue', 'pullRequest'):
                issue = repo.issues.get(str(args.get('number')))
                return self.issue(repo, issue) if issue and ('head' in issue) == pulls else None
            issues = [i for i in repo.issues.values() if ('head' in i) == pulls]
            if args.get('states'):
                issues = [i for i in issues if ('MERGED' if i.get('merged') else i['state'].upper()) in args['states']]
            order = args.get('orderBy') or {}
            issues.sort(key=lambda i: (i['created_at'], i['number']), reverse=order.get('direction', 'DESC') == 'DESC')
            return self.connection('PullRequestConnection' if pulls else 'IssueConnection',
                                   [self.issue(repo, i) for i in issues], args)
        if kind in ('Issue', 'PullRequest'):
            repo, issue = obj['_repo'], obj['_issue']
            values = dict(issue, id=gql_id(kind, issue['id']), state='MERGED' if issue.get('merged') else issue['state'].upper(),
                          createdAt=issue['created_at'], updatedAt=issue['updated_at'],
                          author=gql_object('User', login=LOGIN, id=gql_id('User', 1)), repository=self.repository(repo))
            if kind == 'PullRequest':
                values.update(headRefName=issue['head']['ref'], baseRefName=issue['base']['ref'], mergeable='MERGEABLE',
                              files=self.connection('PullRequestChangedFileConnection', [gql_object('PullRequestChangedFile',
                                  path=p, additions=0, deletions=0, changeType='MODIFIED') for p in self.handler.pull_files(repo, issue)], args))
            return values.get(name)
        if name not in obj:
            raise ValueError(f'Unknown field "{name}" on type "{kind}"')
        return obj[name]

    def select(self, obj, fields, stack=()):
        if obj is None:
            return None
        if isinstance(obj, list):
            return [self.select(item, fields, stack) for item in obj]
        result = {}
        for alias, name, args, children, condition in fields:
            if name.startswith('...'):
                if name != '...':
                    fragment = name[3:]
                    if fragment in stack:
                        raise ValueError('Cyclic fragment ' + fragment)
                    if fragment not in self.parser.fragments:
                        raise ValueError('Unknown fragment ' + fragment)
                    condition, children = self.parser.fragments[fragment]
                    nested_stack = stack + (fragment,)
                else:
                    nested_stack = stack
                if condition in (None, obj['__typename'], 'Node'):
                    result.update(self.select(obj, children, nested_stack))
                continue
            value = self.resolve(obj, name, args)
            result[alias] = self.select(value, children, stack) if children else value
        return result


def handle_graphql(handler, payload):
    if isinstance(payload, list):
        return [handle_graphql(handler, item) for item in payload]
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get('query'), str):
            raise ValueError('query must be a string')
        parser = GraphQLParser(payload['query'], payload.get('variables') or {})
        operations = parser.document()
        if payload.get('operationName'):
            operations = [op for op in operations if op[0] == payload['operationName']]
        if not operations:
            raise ValueError('No matching operation')
        engine = GraphQL(handler, parser)
        results = []
        for _, kind, fields in operations:
            try:
                results.append({'data': engine.select(gql_object('Mutation' if kind == 'mutation' else 'Query'), fields)})
            except (ValueError, TypeError, KeyError, APIError) as error:
                results.append({'data': None, 'errors': [{'message': getattr(error, 'message', str(error))}]})
        return results[0] if len(results) == 1 else results
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        return {'errors': [{'message': str(error)}]}


def rpc_error(identifier, code, message):
    return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': message}}


MCP_TOOLS = [
    {'name': name, 'description': description,
     'inputSchema': {'type': 'object', 'properties': {key: {'type': 'integer' if key == 'pull_number' else 'string'}
                                                    for key in properties.split()}, 'required': required.split()}}
    for name, description, properties, required in [
        ('create_repo', 'Create a repository for the authenticated user.', 'name', 'name'),
        ('get_repo', 'Read repository metadata.', 'owner repo', 'owner repo'),
        ('create_issue', 'Create an issue in a repository.', 'owner repo title body', 'owner repo title'),
        ('list_issues', 'List repository issues filtered by state.', 'owner repo state', 'owner repo'),
        ('create_pull_request', 'Create a pull request between existing branches.', 'owner repo title head base', 'owner repo title head base'),
        ('merge_pull_request', 'Merge a pull request into its base branch.', 'owner repo pull_number', 'owner repo pull_number'),
    ]
]


def handle_mcp(handler, payload):
    if isinstance(payload, list):
        return [handle_mcp(handler, item) for item in payload] if payload else rpc_error(None, -32600, 'Empty batch')
    if not isinstance(payload, dict):
        return rpc_error(None, -32600, 'Invalid request')
    identifier = payload.get('id')
    method, params = payload.get('method'), payload.get('params') or {}
    if not isinstance(params, dict):
        return rpc_error(identifier, -32602, 'params must be an object')
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion') or payload.get('protocolVersion') or '2026-07-28',
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'forge-mcp', 'version': '1.0.0'}}
    elif method == 'notifications/initialized':
        result = {}
    elif method == 'server/discover':
        result = {'supportedVersions': ['2026-07-28']}
    elif method == 'tools/list':
        result = {'tools': MCP_TOOLS}
    elif method == 'tools/call':
        normalize = lambda text: re.sub(r'[_-]', '', text).lower()
        requested = params.get('name', '')
        tool = next((t for t in MCP_TOOLS if isinstance(requested, str) and normalize(requested).endswith(normalize(t['name']))), None)
        if tool is None:
            return rpc_error(identifier, -32601, 'Unknown tool ' + str(requested))
        arguments = params.get('arguments', {})
        if not isinstance(arguments, dict):
            return rpc_error(identifier, -32602, 'arguments must be an object')
        incoming = {normalize(k): v for k, v in arguments.items()}
        args = {k: incoming[normalize(k)] for k in tool['inputSchema']['properties'] if normalize(k) in incoming}
        if isinstance(incoming.get('fullname'), str) and '/' in incoming['fullname']:
            owner, repo = incoming['fullname'].split('/', 1)
            args.setdefault('owner', owner)
            args.setdefault('repo', repo)
        missing = [k for k in tool['inputSchema']['required'] if k not in args or args[k] in (None, '')]
        if missing:
            return rpc_error(identifier, -32602, 'Missing required argument: ' + ', '.join(missing))
        for key, value in args.items():
            expected = int if key == 'pull_number' else str
            if not isinstance(value, expected) or isinstance(value, bool):
                return rpc_error(identifier, -32602, 'Invalid argument: ' + key)
        name = tool['name']
        route = ['api', 'v3', 'repos', args.get('owner', ''), args.get('repo', '')]
        query, verb = {}, 'POST'
        if name == 'create_repo':
            route = ['api', 'v3', 'user', 'repos']
        elif name == 'get_repo':
            verb = 'GET'
        elif name in ('create_issue', 'list_issues'):
            route.append('issues')
            if name == 'list_issues':
                verb, query = 'GET', {'state': [args.get('state', 'open')]}
        elif name == 'create_pull_request':
            route.append('pulls')
        else:
            route.extend(['pulls', str(args['pull_number']), 'merge'])
            verb = 'PUT'
        previous = handler.command
        try:
            handler.command = verb
            _, value, _ = handler.dispatch(route, query, args)
            result = {'content': [{'type': 'text', 'text': json.dumps(value, separators=(',', ':'))}]}
        except APIError as error:
            return rpc_error(identifier, -32602, error.message)
        finally:
            handler.command = previous
    else:
        return rpc_error(identifier, -32601, 'Unknown method ' + str(method))
    return {'jsonrpc': '2.0', 'id': identifier, 'result': result}


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
        body = b"" if status in (202, 204) else json.dumps(
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

    def read_payload(self, allow_batch=False):
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
        if not allow_batch and not isinstance(payload, dict):
            raise APIError(400, "JSON body must be an object")
        return payload

    def read_git_body(self):
        """Git switches to chunked HTTP for pushes larger than http.postBuffer."""
        lengths = self.headers.get_all("Content-Length", [])
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if len(lengths) > 1 or (transfer and lengths):
            raise ValueError("Ambiguous request length")
        result = bytearray()
        if transfer:
            if transfer != "chunked":
                raise ValueError("Unsupported Transfer-Encoding")
            while True:
                line = self.rfile.readline(8192)
                if not line.endswith(b"\r\n"):
                    raise ValueError("Invalid chunk header")
                size = int(line.split(b";", 1)[0], 16)
                if size < 0 or size + len(result) > MAX_GIT_BODY:
                    raise ValueError("Git request too large")
                if not size:
                    trailer_size = 0
                    while True:
                        trailer = self.rfile.readline(8192)
                        trailer_size += len(trailer)
                        if not trailer or trailer_size > 8192:
                            raise ValueError("Invalid chunk trailers")
                        if trailer == b"\r\n":
                            return bytes(result)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("Truncated chunk")
                result.extend(chunk)
        length = int(lengths[0]) if lengths else 0
        if not 0 <= length <= MAX_GIT_BODY:
            raise ValueError("Git request too large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Truncated request")
        return data

    def git_request(self, parts, query):
        try:
            body = self.read_git_body()
            with self.store.lock:
                repo = self.store.repos.get(parts[1][:-4].lower()) if parts[0] == LOGIN else None
                if repo is None:
                    raise APIError(404, "Not Found")
                with repo.lock:
                    if self.command == "GET" and parts[2:] == ["info", "refs"]:
                        service = query.get("service", [""])[0]
                        if service not in ("git-upload-pack", "git-receive-pack"):
                            raise APIError(400, "Unsupported Git service")
                        result = git_advertisement(repo, service,
                                                   "version=2" in self.headers.get("Git-Protocol", "").split(":"))
                        content_type = f"application/x-{service}-advertisement"
                    elif self.command == "POST" and parts[2:] in (["git-receive-pack"], ["git-upload-pack"]):
                        service = parts[2]
                        if service == "git-receive-pack":
                            result = git_receive_pack(repo, body)
                            date = self.store.date()
                            repo.metadata.update(updated_at=date, pushed_at=date)
                        else:
                            result = git_upload_pack(repo, body, query)
                        content_type = f"application/x-{service}-result"
                    else:
                        raise APIError(404, "Not Found")
                if self.command == "POST" and service == "git-receive-pack":
                    self.store.save()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(result)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(result)
        except (ValueError, IndexError, KeyError, StopIteration, OverflowError, zlib.error, struct.error) as error:
            self.close_connection = True
            raise APIError(400, "Invalid Git request: " + str(error))

    def request(self):
        if not self.authorized():
            self.close_connection = True
            self.respond(401, {"message": "Requires authentication"}, {"Connection": "close"})
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.rstrip("/").split("/")[1:]]
            query = urllib.parse.parse_qs(parsed.query)
            if len(parts) >= 3 and parts[1].endswith(".git"):
                self.git_request(parts, query)
                return
            if self.command == 'POST' and parts in (['api', 'graphql'], ['mcp']):
                graphql = parts == ['api', 'graphql']
                try:
                    payload = self.read_payload(allow_batch=True)
                except APIError as error:
                    self.respond(200 if graphql else 400,
                                 {'errors': [{'message': error.message}]} if graphql else
                                 rpc_error(None, -32700, error.message))
                    return
                with self.store.lock:
                    result = handle_graphql(self, payload) if graphql else handle_mcp(self, payload)
                    self.store.save()
                    notification = not graphql and isinstance(payload, dict) and payload.get('method') == 'notifications/initialized'
                    self.respond(202 if notification else 200, result)
                return
            payload = self.read_payload()
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
        if route == ['pulls']:
            if method == 'POST':
                return 201, self.create_pull(repo, payload), {}
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                return self.paginate([i for i in repo.issues.values() if 'head' in i and
                                      (state == 'all' or i['state'] == state)], query)
        if len(route) >= 2 and route[0] == 'pulls':
            issue = repo.issues.get(route[1])
            if not issue or 'head' not in issue:
                raise APIError(404, 'Pull request not found')
            if len(route) == 2 and method == 'GET':
                return 200, issue, {}
            if route[2:] == ['merge'] and method == 'PUT':
                self.merge_pull(repo, issue)
                return 200, {'merged': True, 'sha': issue['merge_commit_sha'], 'message': 'Pull request successfully merged'}, {}
            if route[2:] == ['files'] and method == 'GET':
                return self.paginate([{'filename': p, 'status': 'modified'} for p in self.pull_files(repo, issue)], query)
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
        if len(route) == 2 and route[0] == "issues" and method in ("GET", "PATCH"):
            issue = repo.issues.get(route[1])
            if issue is None:
                raise APIError(404, "Not Found")
            if method == 'PATCH':
                if 'state' in payload and payload['state'] not in ('open', 'closed'):
                    raise APIError(422, 'Invalid state')
                for key in ('title', 'body'):
                    if key in payload and not isinstance(payload[key], str):
                        raise APIError(422, 'Invalid ' + key)
                issue.update({k: payload[k] for k in ('title', 'body', 'state') if k in payload})
                issue['updated_at'] = self.store.date()
                issue['closed_at'] = issue['updated_at'] if issue['state'] == 'closed' else None
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

    def create_pull(self, repo, payload):
        title = required_text(payload, 'title')
        head = required_text(payload, 'head')
        base = required_text(payload, 'base')
        if ':' in head:
            owner, head = head.split(':', 1)
            if owner != LOGIN:
                raise APIError(422, 'Unknown head repository')
        if head == base or head not in repo.refs or base not in repo.refs:
            raise APIError(422, 'Head and base must be distinct existing branches')
        number, date = len(repo.issues) + 1, self.store.date()
        issue = {'id': self.store.identifier(), 'number': number, 'title': title, 'body': payload.get('body'),
                 'state': 'open', 'merged': False, 'mergeable': True, 'merge_commit_sha': None,
                 'head': {'ref': head, 'sha': repo.refs[head]}, 'base': {'ref': base, 'sha': repo.refs[base]},
                 'created_at': date, 'updated_at': date, 'closed_at': None, 'merged_at': None,
                 'user': {'login': LOGIN}, 'labels': [], 'comments': 0,
                 'url': self.base + '/repos/' + repo.metadata['full_name'] + '/pulls/' + str(number)}
        issue['node_id'] = gql_id('PullRequest', issue['id'])
        issue['pull_request'] = {'url': issue['url']}
        repo.issues[str(number)] = issue
        return issue

    def pull_merge_snapshot(self, repo, issue):
        head = repo.resolve(issue['head']['ref'])
        base = repo.resolve(issue['base']['ref'])

        def ancestors(start):
            result, queue = set(), [start]
            while queue:
                sha = queue.pop()
                if sha not in result:
                    result.add(sha)
                    _, parents = commit_headers(split_object(repo.objects[sha])[1])
                    queue.extend(parents)
            return result

        head_ancestors = ancestors(head)
        queue, seen, common = [base], set(), None
        while queue:
            sha = queue.pop(0)
            if sha in head_ancestors:
                common = sha
                break
            if sha not in seen:
                seen.add(sha)
                _, parents = commit_headers(split_object(repo.objects[sha])[1])
                queue.extend(parents)
        previous = repo.commits[common]['files'] if common else {}
        source, target = repo.commits[head]['files'], repo.commits[base]['files']
        files, changed = dict(target), []
        for path in sorted(previous.keys() | source.keys()):
            if source.get(path) == previous.get(path):
                continue
            if target.get(path) not in (previous.get(path), source.get(path)):
                raise APIError(405, 'Merge conflict in ' + path)
            if source.get(path) != target.get(path):
                changed.append(path)
            if path in source:
                files[path] = source[path]
            else:
                files.pop(path, None)
        return files, changed, head, base

    def pull_files(self, repo, issue):
        source = repo.commits[repo.resolve(issue['head']['ref'])]['files']
        target = repo.commits[issue['base']['sha']]['files']
        return sorted(p for p in source.keys() | target.keys() if source.get(p) != target.get(p))

    def merge_pull(self, repo, issue):
        if issue['merged']:
            return
        if issue['state'] != 'open':
            raise APIError(409, 'Pull request is closed')
        files, changed, _, _ = self.pull_merge_snapshot(repo, issue)
        date = self.store.date()
        branch = issue['base']['ref']
        sha = repo.commit(files, 'Merge pull request #' + str(issue['number']) + ': ' + issue['title'],
                          branch, date, changed)
        # repo.commit uses only the current base tip as parent: a squash merge.
        issue.update(merged=True, state='closed', merged_at=date, closed_at=date, updated_at=date,
                     merge_commit_sha=sha)

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
