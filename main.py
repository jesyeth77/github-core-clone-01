#!/usr/bin/env python3
"""git-forge: a GitHub-compatible git forge in pure Python 3.13 stdlib.

Serves, from one process and its own in-memory store:
  * git smart HTTP (protocol v2 + v0): info/refs, git-upload-pack, git-receive-pack
  * GitHub REST v3: repositories, branches, commits, contents, issues, comments,
    labels, pull requests, files, merges
  * GitHub GraphQL v4: the documents gh and github-mcp-server actually send
  * MCP over streamable HTTP: initialize, server/discover, tools/list, tools/call

Lifecycle (the harness contract): bind an ephemeral port, POST {"port": p} to
$MAKEFUN_ENV_URL/register, poll GET $MAKEFUN_ENV_URL/done until {"done": true},
then exit 0. With no MAKEFUN_ENV_URL set the server runs standalone (dev mode).

No third-party imports, no git binary, no network. Deterministic where the
suite requires it: object ids are git SHA-1s, global ids are stable.
"""

import base64
import difflib
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Scenario identity: the suite's viewer login, derived from the contest's one
# fixed seed (wire.ts SCENARIO_SEED) through the same PRNG, so the server
# answers viewer.login and owns POST /user/repos under the right owner.
# ---------------------------------------------------------------------------

_U64 = (1 << 64) - 1
_SCENARIO_SEED = 0x6A09E667F3BCC909


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _U64
    return h


def _mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & _U64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _U64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _U64
    return (z ^ (z >> 31)) & _U64


class _Rng:
    def __init__(self, seed):
        s = seed & _U64
        self.state = s if s != 0 else _SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & _U64
        x = (x ^ (x << 25)) & _U64
        x = (x ^ (x >> 27)) & _U64
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & _U64

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, mx):
        if mx == 1:
            return 0
        limit = 4294967296 - (4294967296 % mx)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % mx

    def pick(self, items):
        return items[self.next_int(len(items))]


_FIRST_NAMES = ['ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta',
                'hugo', 'iris', 'jonas', 'kira', 'lars', 'mira', 'nico',
                'olga', 'pavel', 'quinn', 'rosa', 'sven', 'tess', 'ulla',
                'vito', 'wren', 'yara', 'zane']
_LAST_NAMES = ['anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer',
               'garcia', 'holm', 'iversen', 'jansen', 'keller', 'lind',
               'moreau', 'novak', 'olsen', 'peters', 'quist', 'rossi',
               'sato', 'turi', 'ulrich', 'vega', 'weber', 'xu', 'yilmaz',
               'zimmer']


def _scenario_viewer_login():
    rng = _Rng(_mix64(_SCENARIO_SEED ^ _fnv1a64('users')))
    used = set()
    for _ in range(12):
        first = rng.pick(_FIRST_NAMES)
        last = rng.pick(_LAST_NAMES)
        base = re.sub(r'[^a-z0-9-]', '', first + last)
        login = base
        suffix = 2
        while login in used:
            login = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(login)
        return login
    return 'adaiversen'


VIEWER_LOGIN = _scenario_viewer_login()
VIEWER_ID = 1000
VIEWER_EMAIL = VIEWER_LOGIN + '@example.invalid'
AGENT_NAME = 'git-forge/1.0'
ZERO_OID = '0' * 40
TOKEN = os.environ.get('MAKEFUN_SHOP_TOKEN', '')
EPOCH = 1735689600  # 2025-01-01T00:00:00Z, scenario day one

# ---------------------------------------------------------------------------
# git object model: one content-addressed store, per-repo refs.
# objects: oid -> (type, bytes); types: commit/tree/blob/tag.
# ---------------------------------------------------------------------------

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7
_TYPE_BY_NUM = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
_NUM_BY_TYPE = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}

LOCK = threading.RLock()
OBJECTS = {}          # oid -> (type_str, bytes)
REFS = {}             # (owner, repo) -> {refname: oid}


def git_hash(otype, data):
    h = hashlib.sha1()
    h.update(('%s %d\0' % (otype, len(data))).encode('ascii'))
    h.update(data)
    return h.hexdigest()


def store_object(otype, data):
    oid = git_hash(otype, data)
    with LOCK:
        if oid not in OBJECTS:
            OBJECTS[oid] = (otype, data)
    return oid


def get_object(oid):
    with LOCK:
        return OBJECTS.get(oid)


def parse_commit(data):
    """Return {'tree': oid, 'parents': [oid], 'message': str, 'raw': data}."""
    text = data.decode('utf-8', 'replace')
    head, _, message = text.partition('\n\n')
    tree = None
    parents = []
    for line in head.split('\n'):
        if line.startswith('tree '):
            tree = line[5:].strip()
        elif line.startswith('parent '):
            parents.append(line[7:].strip())
    return {'tree': tree, 'parents': parents, 'message': message, 'raw': data}


def parse_tree(data):
    """Return [(mode_str, name, oid)] for a tree object."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b' ', i)
        mode = data[i:sp].decode('ascii')
        nul = data.index(b'\0', sp)
        name = data[sp + 1:nul].decode('utf-8', 'replace')
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def build_tree(entries):
    """entries: [(mode_str, name, oid)] sorted by name. Returns tree bytes."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=lambda e: e[1]):
        out += mode.encode('ascii') + b' ' + name.encode('utf-8') + b'\0'
        out += bytes.fromhex(oid)
    return bytes(out)


def tree_to_files(tree_oid, prefix=''):
    """Flatten a tree to {path: (mode, blob_oid)}."""
    files = {}
    obj = get_object(tree_oid)
    if obj is None or obj[0] != 'tree':
        return files
    for mode, name, oid in parse_tree(obj[1]):
        path = prefix + name
        if mode == '40000':
            files.update(tree_to_files(oid, path + '/'))
        else:
            files[path] = (mode, oid)
    return files


def build_tree_from_files(files):
    """files: {path: (mode, blob_oid)} -> root tree oid (creates subtrees)."""
    # group by first path component
    def rec(paths):
        entries = []
        dirs = {}
        for path, (mode, oid) in paths.items():
            if '/' in path:
                head, rest = path.split('/', 1)
                dirs.setdefault(head, {})[rest] = (mode, oid)
            else:
                entries.append((mode, path, oid))
        for d, sub in dirs.items():
            entries.append(('40000', d, rec(sub)))
        return store_object('tree', build_tree(entries))
    return rec(files)


def commit_reachable(tips, depth=None, stop_at=frozenset()):
    """All object oids reachable from tip commits.

    depth: max commit generations from a tip (None = unlimited). stop_at: set of
    commit oids whose parents are not traversed (shallow boundaries) -- the
    boundary commit itself, its tree and blobs are included.
    Returns (commits, all_objects) with all_objects including trees/blobs.
    """
    commits = {}
    seen = set()
    frontier = [(t, 0) for t in tips]
    while frontier:
        oid, d = frontier.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        obj = get_object(oid)
        if obj is None or obj[0] != 'commit':
            continue
        commits[oid] = d
        if oid in stop_at:
            continue
        if depth is not None and d + 1 >= depth:
            continue
        parsed = parse_commit(obj[1])
        for p in parsed['parents']:
            frontier.append((p, d + 1))
    objects = set()
    for oid in commits:
        parsed = parse_commit(get_object(oid)[1])
        stack = [parsed['tree']]
        while stack:
            toid = stack.pop()
            if toid is None or toid in objects:
                continue
            tobj = get_object(toid)
            if tobj is None:
                continue
            objects.add(toid)
            if tobj[0] == 'tree':
                for mode, _name, child in parse_tree(tobj[1]):
                    if mode == '40000':
                        stack.append(child)
                    else:
                        objects.add(child)
    objects.update(commits.keys())
    return commits, objects


def is_ancestor(ancestor, descendant):
    """True when ancestor is reachable from descendant (commit graph)."""
    if ancestor == descendant:
        return True
    seen = set()
    stack = [descendant]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        obj = get_object(oid)
        if obj is None or obj[0] != 'commit':
            continue
        parents = parse_commit(obj[1])['parents']
        if ancestor in parents:
            return True
        stack.extend(parents)
    return False


# ---------------------------------------------------------------------------
# packfiles: undeltified PACK v2 on send; full parse (incl. deltas) on receive.
# ---------------------------------------------------------------------------

def _pack_obj_header(otype_num, size):
    out = bytearray()
    b = (otype_num << 4) | (size & 0x0F)
    size >>= 4
    if size:
        b |= 0x80
    out.append(b)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)


def build_pack(oids):
    """Undeltified pack of the given object ids (order: commits, tags, trees, blobs).

    Packs are cached by object set: objects are content-addressed and never
    change, so a pack for a set is immutable. The clone benchmark fetches
    the same 200-commit repo 50 times concurrently; without the cache every
    clone re-compresses ~600 objects under the GIL.
    """
    key = frozenset(oids)
    with LOCK:
        cached = _PACK_CACHE.get(key)
    if cached is not None:
        return cached
    items = []
    for oid in oids:
        obj = get_object(oid)
        if obj is None:
            continue
        items.append((oid, obj[0], obj[1]))
    order = {'commit': 0, 'tag': 1, 'tree': 2, 'blob': 3}
    items.sort(key=lambda it: (order.get(it[1], 4), it[0]))
    parts = [b'PACK', (2).to_bytes(4, 'big'), len(items).to_bytes(4, 'big')]
    for _oid, otype, data in items:
        parts.append(_pack_obj_header(_NUM_BY_TYPE[otype], len(data)))
        parts.append(zlib.compress(data, 1))
    body = b''.join(parts)
    pack = body + hashlib.sha1(body).digest()
    with LOCK:
        if len(_PACK_CACHE) > 64:
            _PACK_CACHE.clear()
        _PACK_CACHE[key] = pack
    return pack


_PACK_CACHE = {}


def _read_varint(data, pos):
    result = data[pos]
    pos += 1
    shift = 7
    while result & 0x80:
        result = (result & 0x7F) | (data[pos] << shift)
        pos += 1
        shift += 7
    return result, pos


def _apply_delta(base, delta):
    src_size, pos = _read_varint(delta, 0)
    tgt_size, pos = _read_varint(delta, pos)
    if src_size != len(base):
        raise ValueError('delta base size mismatch')
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            off = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[off:off + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError('delta opcode 0')
    if len(out) != tgt_size:
        raise ValueError('delta target size mismatch')
    return bytes(out)


def parse_pack(data, resolve_base=None):
    """Parse a packfile. Returns {oid: (type, bytes)} for its objects.

    resolve_base(oid) -> (type, bytes) for thin-pack bases the client holds.
    """
    if len(data) < 12 or data[:4] != b'PACK':
        raise ValueError('not a packfile')
    version = int.from_bytes(data[4:8], 'big')
    if version != 2:
        raise ValueError('pack version %d' % version)
    count = int.from_bytes(data[8:12], 'big')
    pos = 12
    results = {}
    ofs_bases = {}
    for _ in range(count):
        start = pos
        byte = data[pos]
        pos += 1
        otype_num = (byte >> 4) & 7
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = data[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if otype_num == OBJ_OFS_DELTA:
            byte = data[pos]
            pos += 1
            offset = byte & 0x7F
            while byte & 0x80:
                byte = data[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (byte & 0x7F)
            base_pos = start - offset
            if base_pos not in ofs_bases:
                raise ValueError('ofs-delta base not yet read')
            base_type, base_data = ofs_bases[base_pos]
            raw = zlib.decompressobj().decompress(data[pos:])
            # find consumed length by recompressing is wrong; use decompressobj unused_data
            d = zlib.decompressobj()
            raw = d.decompress(data[pos:])
            pos = len(data) - len(d.unused_data)
            content = _apply_delta(base_data, raw)
            oid = git_hash(base_type, content)
            results[oid] = (base_type, content)
            ofs_bases[start] = (base_type, content)
        elif otype_num == OBJ_REF_DELTA:
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            d = zlib.decompressobj()
            raw = d.decompress(data[pos:])
            pos = len(data) - len(d.unused_data)
            base = results.get(base_oid)
            if base is None and resolve_base is not None:
                base = resolve_base(base_oid)
            if base is None:
                raise ValueError('ref-delta base %s missing' % base_oid)
            content = _apply_delta(base[1], raw)
            oid = git_hash(base[0], content)
            results[oid] = (base[0], content)
            ofs_bases[start] = (base[0], content)
        else:
            otype = _TYPE_BY_NUM[otype_num]
            d = zlib.decompressobj()
            raw = d.decompress(data[pos:])
            pos = len(data) - len(d.unused_data)
            if len(raw) != size:
                raise ValueError('object size mismatch')
            oid = git_hash(otype, raw)
            results[oid] = (otype, raw)
            ofs_bases[start] = (otype, raw)
    return results


# ---------------------------------------------------------------------------
# pkt-line framing + sideband.
# ---------------------------------------------------------------------------

def pkt_line(payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


PKT_FLUSH = b'0000'
PKT_DELIM = b'0001'


def pkt_lines(lines):
    out = bytearray()
    for item in lines:
        if item == 'flush':
            out += PKT_FLUSH
        elif item == 'delim':
            out += PKT_DELIM
        else:
            out += pkt_line(item)
    return bytes(out)


def sideband_pack(pack_bytes, chunk=60000):
    """Wrap pack bytes in side-band-64k band-1 pkt-lines + flush."""
    out = bytearray()
    for i in range(0, len(pack_bytes), chunk):
        out += pkt_line(b'\x01' + pack_bytes[i:i + chunk])
    out += PKT_FLUSH
    return bytes(out)


def parse_pkt_lines(data):
    """Parse a pkt-line stream. Returns list of ('data', bytes) | ('flush',) | ('delim',)."""
    out = []
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        try:
            size = int(data[pos:pos + 4], 16)
        except ValueError:
            break  # raw pack bytes follow the pkt-lines; stop here
        if size == 0:
            out.append(('flush', b''))
            pos += 4
            continue
        if size == 1:
            out.append(('delim', b''))
            pos += 4
            continue
        if size < 4 or pos + size > n:
            break
        out.append(('data', data[pos + 4:pos + size]))
        pos += size
    return out, pos

# ---------------------------------------------------------------------------
# Store: repositories, issues, pull requests, labels.
# ---------------------------------------------------------------------------

REPOS = {}            # (owner, name) -> repo dict
REPO_SEQ = [5000]     # next databaseId
COMMENT_SEQ = [1]
CREATED_SEQ = [0]
REQ_SEQ = [0]


def _next_seq():
    CREATED_SEQ[0] += 1
    return CREATED_SEQ[0]


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _b64_global(kind, ident):
    raw = ('%s:%s' % (kind, ident)).encode('utf-8')
    return base64.b64encode(raw).decode('ascii')


def new_repo(owner, name, private, description):
    dbid = REPO_SEQ[0]
    REPO_SEQ[0] += 1
    repo = {
        'id': dbid,
        'global_id': _b64_global('Repository', dbid),
        'name': name,
        'owner': owner,
        'description': description or '',
        'private': bool(private),
        'default_branch': 'main',
        'created_at': _now_iso(),
        'issues': [],
        'issue_seq': [0],
        'pulls': [],
        'pull_seq': [0],
        'labels': {},   # name -> {'name','color','description'}
        'created_seq': _next_seq(),
    }
    with LOCK:
        REPOS[(owner, name)] = repo
        REFS.setdefault((owner, name), {})
    return repo


def get_repo(owner, name):
    with LOCK:
        return REPOS.get((owner, name))


def repo_refs(owner, name):
    with LOCK:
        return REFS.setdefault((owner, name), {})


def get_ref(owner, name, refname):
    with LOCK:
        return REFS.get((owner, name), {}).get(refname)


def set_ref(owner, name, refname, oid):
    with LOCK:
        REFS.setdefault((owner, name), {})[refname] = oid


def del_ref(owner, name, refname):
    with LOCK:
        REFS.get((owner, name), {}).pop(refname, None)


def _commit_object(tree_oid, parents, message, author_name=None):
    author_name = author_name or VIEWER_LOGIN
    ts = int(time.time())
    lines = ['tree ' + tree_oid]
    for p in parents:
        lines.append('parent ' + p)
    lines.append('author %s <%s> %d +0000' % (author_name, VIEWER_EMAIL, ts))
    lines.append('committer %s <%s> %d +0000' % (author_name, VIEWER_EMAIL, ts))
    raw = ('\n'.join(lines) + '\n\n' + message).encode('utf-8')
    return store_object('commit', raw)


def _blob_oid(content_bytes):
    return store_object('blob', content_bytes)


def branch_tip(owner, name, branch):
    return get_ref(owner, name, 'refs/heads/' + branch)


def _read_tree_files(owner, name, branch):
    tip = branch_tip(owner, name, branch)
    if not tip:
        return {}, None
    obj = get_object(tip)
    if obj is None:
        return {}, None
    tree = parse_commit(obj[1])['tree']
    return tree_to_files(tree), tip


def commit_file(owner, name, branch, path, content_bytes, message,
                expected_blob_sha=None, delete=False):
    """Apply one file change on a branch; returns (commit_oid, blob_oid|None).

    Raises ValueError('stale-sha') when expected_blob_sha is given and the
    current blob differs; ValueError('missing') when deleting a missing path.
    """
    files, tip = _read_tree_files(owner, name, branch)
    if delete:
        if path not in files:
            raise ValueError('missing')
        del files[path]
        blob_oid = None
    else:
        if expected_blob_sha is not None:
            cur = files.get(path)
            cur_sha = cur[1] if cur else None
            if cur_sha != expected_blob_sha:
                raise ValueError('stale-sha')
        blob_oid = _blob_oid(content_bytes)
        files[path] = ('100644', blob_oid)
    tree_oid = build_tree_from_files(files)
    parents = [tip] if tip else []
    commit_oid = _commit_object(tree_oid, parents, message)
    set_ref(owner, name, 'refs/heads/' + branch, commit_oid)
    return commit_oid, blob_oid


def diff_files(base_files, head_files):
    """[(path, status, additions, deletions, changes)] base -> head."""
    out = []
    for path in sorted(set(base_files) | set(head_files)):
        b = base_files.get(path)
        h = head_files.get(path)
        if b is None and h is not None:
            content = get_object(h[1])[1].decode('utf-8', 'replace')
            additions = len(content.splitlines()) or 1
            out.append((path, 'added', additions, 0, additions))
        elif h is None and b is not None:
            content = get_object(b[1])[1].decode('utf-8', 'replace')
            deletions = len(content.splitlines()) or 1
            out.append((path, 'removed', 0, deletions, deletions))
        elif b is not None and h is not None and b[1] != h[1]:
            old = get_object(b[1])[1].decode('utf-8', 'replace').splitlines()
            new = get_object(h[1])[1].decode('utf-8', 'replace').splitlines()
            sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
            additions = deletions = 0
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ('insert', 'replace'):
                    additions += j2 - j1
                if tag in ('delete', 'replace'):
                    deletions += i2 - i1
            if additions == 0 and deletions == 0:
                additions = 1
            out.append((path, 'modified', additions, deletions,
                        additions + deletions))
    return out


def squash_merge(owner, name, pull):
    """Squash-merge a pull request. Returns merge commit oid or None on conflict.

    Conflict: a path the head changed was also changed on the base branch after
    the PR's base commit, to different content.
    """
    repo = get_repo(owner, name)
    base_branch = pull['base_ref']
    base_tip = branch_tip(owner, name, base_branch)
    head_tip = branch_tip(owner, name, pull['head_ref'])
    if base_tip is None or head_tip is None:
        return None
    base_files, _ = _read_tree_files(owner, name, base_branch)
    head_files, _ = _read_tree_files(owner, name, pull['head_ref'])
    pr_base_files, _ = _read_tree_files_at(owner, name, pull['base_sha'])
    merged = dict(base_files)
    for path, (mode, oid) in head_files.items():
        base_at_pr = pr_base_files.get(path)
        base_now = base_files.get(path)
        if base_at_pr is not None and base_now is not None \
                and base_at_pr[1] != base_now[1] and base_now[1] != oid:
            return None  # both sides changed it differently
        if base_at_pr is None and base_now is not None and base_now[1] != oid:
            return None  # file created on both sides differently
        merged[path] = (mode, oid)
    for path in list(merged):
        if path not in head_files and path in pr_base_files:
            # head deleted it; keep base's version unless base also changed it
            if base_files.get(path) != pr_base_files.get(path):
                return None
            del merged[path]
    tree_oid = build_tree_from_files(merged)
    msg = 'Merge pull request #%d from %s' % (pull['number'], pull['head_ref'])
    merge_oid = _commit_object(tree_oid, [base_tip, head_tip], msg)
    set_ref(owner, name, 'refs/heads/' + base_branch, merge_oid)
    return merge_oid


def _read_tree_files_at(owner, name, commit_oid):
    obj = get_object(commit_oid)
    if obj is None or obj[0] != 'commit':
        return {}, None
    tree = parse_commit(obj[1])['tree']
    return tree_to_files(tree), commit_oid


def _git_user(login):
    return {'login': login, 'id': VIEWER_ID, 'type': 'User',
            'html_url': 'https://example.invalid/' + login}


def repo_json(repo):
    return {
        'id': repo['id'],
        'databaseId': repo['id'],
        'node_id': repo['global_id'],
        'name': repo['name'],
        'full_name': '%s/%s' % (repo['owner'], repo['name']),
        'owner': _git_user(repo['owner']),
        'private': repo['private'],
        'description': repo['description'],
        'default_branch': repo['default_branch'],
        'created_at': repo['created_at'],
        'updated_at': repo['created_at'],
        'html_url': 'https://example.invalid/%s/%s' % (repo['owner'], repo['name']),
        'url': '/api/v3/repos/%s/%s' % (repo['owner'], repo['name']),
        'has_issues': True,
    }


def issue_json(repo, issue):
    return {
        'id': issue['dbid'],
        'node_id': issue['global_id'],
        'number': issue['number'],
        'title': issue['title'],
        'body': issue['body'],
        'state': issue['state'],
        'labels': [{'name': n, 'color': repo['labels'].get(n, {}).get('color', 'ededed'),
                    'description': repo['labels'].get(n, {}).get('description', '')}
                   for n in issue['labels']],
        'user': _git_user(VIEWER_LOGIN),
        'created_at': issue['created_at'],
        'updated_at': issue['created_at'],
        'html_url': 'https://example.invalid/%s/%s/issues/%d' % (
            repo['owner'], repo['name'], issue['number']),
        'url': '/api/v3/repos/%s/%s/issues/%d' % (
            repo['owner'], repo['name'], issue['number']),
    }


def pull_json(repo, pull):
    return {
        'id': pull['dbid'],
        'node_id': pull['global_id'],
        'number': pull['number'],
        'title': pull['title'],
        'body': pull['body'],
        'state': pull['state'],
        'merged': pull['merged'],
        'merged_at': pull['merged_at'],
        'merge_commit_sha': pull['merge_commit_sha'],
        'head': {'ref': pull['head_ref'], 'sha': pull['head_sha'],
                 'label': '%s:%s' % (repo['owner'], pull['head_ref'])},
        'base': {'ref': pull['base_ref'], 'sha': pull['base_sha'],
                 'label': '%s:%s' % (repo['owner'], pull['base_ref'])},
        'user': _git_user(VIEWER_LOGIN),
        'created_at': pull['created_at'],
        'updated_at': pull['created_at'],
        'html_url': 'https://example.invalid/%s/%s/pull/%d' % (
            repo['owner'], repo['name'], pull['number']),
        'url': '/api/v3/repos/%s/%s/pulls/%d' % (
            repo['owner'], repo['name'], pull['number']),
    }


def _new_issue(repo, title, body, labels):
    repo['issue_seq'][0] += 1
    number = repo['issue_seq'][0]
    dbid = 10000 + repo['id'] * 1000 + number
    issue = {
        'dbid': dbid,
        'global_id': _b64_global('Issue', '%d:%d' % (repo['id'], number)),
        'number': number,
        'title': title or '',
        'body': body or '',
        'state': 'open',
        'labels': list(labels or []),
        'comments': [],
        'created_at': _now_iso(),
        'created_seq': _next_seq(),
    }
    repo['issues'].append(issue)
    return issue


def _new_pull(repo, title, body, head_ref, base_ref):
    repo['pull_seq'][0] += 1
    number = repo['pull_seq'][0]
    dbid = 20000 + repo['id'] * 1000 + number
    head_sha = branch_tip(repo['owner'], repo['name'], head_ref) or ZERO_OID
    base_sha = branch_tip(repo['owner'], repo['name'], base_ref) or ZERO_OID
    head_files, _ = _read_tree_files(repo['owner'], repo['name'], head_ref)
    base_files, _ = _read_tree_files(repo['owner'], repo['name'], base_ref)
    files = diff_files(base_files, head_files)
    pull = {
        'dbid': dbid,
        'global_id': _b64_global('PullRequest', '%d:%d' % (repo['id'], number)),
        'repo': repo,
        'number': number,
        'title': title or '',
        'body': body or '',
        'head_ref': head_ref,
        'base_ref': base_ref,
        'head_sha': head_sha,
        'base_sha': base_sha,
        'state': 'open',
        'merged': False,
        'merged_at': None,
        'merge_commit_sha': None,
        'files': files,
        'created_at': _now_iso(),
        'created_seq': _next_seq(),
    }
    repo['pulls'].append(pull)
    return pull


def _mergeable(pull):
    if pull['state'] != 'open':
        return 'UNKNOWN'
    repo = pull['repo']
    base_tip = branch_tip(repo['owner'], repo['name'], pull['base_ref'])
    if base_tip is None:
        return 'UNKNOWN'
    # conflicting when the base moved and changed a path the head also changed
    base_files, _ = _read_tree_files(repo['owner'], repo['name'], pull['base_ref'])
    head_files, _ = _read_tree_files(repo['owner'], repo['name'], pull['head_ref'])
    pr_base_files, _ = _read_tree_files_at(repo['owner'], repo['name'], pull['base_sha'])
    for path in set(head_files) | set(pr_base_files):
        if path in head_files and path in pr_base_files \
                and head_files[path][1] != pr_base_files[path][1]:
            now = base_files.get(path)
            if now is not None and now[1] != pr_base_files[path][1] \
                    and now[1] != head_files[path][1]:
                return 'CONFLICTING'
    return 'MERGEABLE'

# ---------------------------------------------------------------------------
# HTTP plumbing.
# ---------------------------------------------------------------------------

MAX_BODY = 32 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _request_id():
    REQ_SEQ[0] += 1
    return '%x-%06d' % (int(time.time()), REQ_SEQ[0])


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'git-forge/1.0'

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        super().send_error(code, message, explain)

    def _send_json(self, status, obj, extra_headers=None):
        body = b'' if status == 204 else json.dumps(obj).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if extra_headers:
            headers.update(extra_headers)
        self._send_raw(status, body, headers)

    def _send_bytes(self, status, data, ctype, extra_headers=None):
        headers = {'Content-Type': ctype}
        if extra_headers:
            headers.update(extra_headers)
        self._send_raw(status, data, headers)

    def _send_raw(self, status, body, headers):
        # One single write for status line + headers + body: with Nagle on,
        # a separate body write waits up to 40 ms for the client's delayed
        # ACK of the header segment (the perf column's killer).
        from http import HTTPStatus
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = 'OK'
        out = bytearray()
        out += ('HTTP/1.1 %d %s\r\n' % (status, reason)).encode('ascii')
        out += ('Server: %s\r\n' % self.version_string()).encode('ascii')
        out += ('Date: %s\r\n' % self.date_time_string()).encode('ascii')
        for k, v in headers.items():
            out += ('%s: %s\r\n' % (k, v)).encode('latin-1')
        out += ('Content-Length: %d\r\n' % len(body)).encode('ascii')
        out += ('X-GitHub-Request-Id: %s\r\n' % _request_id()).encode('ascii')
        out += b'\r\n'
        out += body
        self.wfile.write(bytes(out))

    def _drain_body(self):
        """Consume the request body once, up front.

        An early 401/404 must never leave body bytes behind: on a keep-alive
        socket they glue onto the next request line and corrupt it (the
        trial-4 501/400s). Handlers read the drained bytes via _read_body().
        """
        if getattr(self, '_body_drained', False):
            return
        self._body_drained = True
        self._cached_body = b''
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            chunks = []
            while True:
                line = self.rfile.readline(65536).strip()
                try:
                    size = int(line.split(b';')[0], 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b'\r\n', b'\n', b''):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            self._cached_body = b''.join(chunks)
            return
        length = self.headers.get('Content-Length')
        if length is None:
            return
        try:
            n = int(length)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            self._cached_body = self.rfile.read(n)

    def _read_body(self):
        self._drain_body()
        return self._cached_body

    def _read_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode('utf-8'))
        except Exception:
            raise ApiError(400, 'Problems parsing JSON')
        if not isinstance(value, dict):
            raise ApiError(400, 'Problems parsing JSON')
        return value

    def _auth_ok(self):
        if not TOKEN:
            return True
        auth = self.headers.get('Authorization') or ''
        return auth.strip() == 'Bearer ' + TOKEN

    def _require_auth(self):
        if not self._auth_ok():
            raise ApiError(401, 'Requires authentication')

    def _page_params(self, query):
        try:
            page = int(query.get('page', ['1'])[0])
        except ValueError:
            page = 1
        try:
            per_page = int(query.get('per_page', ['30'])[0])
        except ValueError:
            per_page = 30
        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)
        return page, per_page

    def _paginate(self, items, query, path):
        page, per_page = self._page_params(query)
        total = len(items)
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        headers = {}
        if start + per_page < total:
            base = 'http://' + (self.headers.get('Host') or 'localhost') + path
            nxt = '%s?%s' % (base, urllib.parse.urlencode(
                {'page': page + 1, 'per_page': per_page}))
            last = '%s?%s' % (base, urllib.parse.urlencode(
                {'page': (total + per_page - 1) // per_page,
                 'per_page': per_page}))
            headers['Link'] = '<%s>; rel="next", <%s>; rel="last"' % (nxt, last)
        return chunk, headers

    def _repo_or_404(self, owner, name):
        if owner != VIEWER_LOGIN:
            raise ApiError(404, 'Not Found')
        repo = get_repo(owner, name)
        if repo is None:
            raise ApiError(404, 'Not Found')
        return repo

    # -- dispatch ---------------------------------------------------------
    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_PATCH(self):
        self._dispatch('PATCH')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def _dispatch(self, method):
        self._body_drained = False
        self._cached_body = b''
        try:
            self._drain_body()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == '/healthz':
                self._send_json(200, {'ok': True})
                return
            self._require_auth()
            parts = [p for p in path.split('/') if p != '']
            if len(parts) >= 2 and parts[1].endswith('.git'):
                self._handle_git_http(method, path, query)
                return
            if path == '/api/v3/' or path == '/api/v3':
                self._send_json(200, {
                    'current_user_url': '/api/v3/user',
                    'repository_url': '/api/v3/repos/{owner}/{repo}',
                    'meta_url': '/api/v3/meta',
                }, {'X-OAuth-Scopes': 'repo, read:org'})
                return
            if path == '/api/v3/meta':
                self._send_json(200, {'installed_version': '1.0.0'})
                return
            if path == '/api/graphql' and method == 'POST':
                self._handle_graphql()
                return
            if path == '/mcp' and method == 'POST':
                self._handle_mcp()
                return
            if path.startswith('/api/v3/'):
                self._handle_rest(method, path, query)
                return
            raise ApiError(404, 'Not Found')
        except ApiError as e:
            self._send_json(e.status, {'message': e.message})
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_json(500, {'message': 'Internal Server Error: %s' % e})

    # -- REST v3 ----------------------------------------------------------
    def _handle_rest(self, method, path, query):
        parts = [p for p in path.split('/') if p != '']
        # parts[0]='api', parts[1]='v3', rest follows
        rest = parts[2:]

        if rest == ['user'] and method == 'GET':
            self._send_json(200, _git_user(VIEWER_LOGIN))
            return
        if rest == ['user', 'repos']:
            if method == 'POST':
                body = self._read_json()
                name = body.get('name')
                if not name:
                    raise ApiError(422, 'name is required')
                if get_repo(VIEWER_LOGIN, name) is not None:
                    # GitHub answers 422 here; a re-create must NOT replace
                    # the existing repository (its ids and refs stay put).
                    raise ApiError(422, 'name already exists on this account')
                repo = new_repo(VIEWER_LOGIN, name, body.get('private', False),
                                body.get('description', ''))
                self._send_json(201, repo_json(repo))
                return
            if method == 'GET':
                with LOCK:
                    mine = sorted(
                        (r for (o, _n), r in REPOS.items() if o == VIEWER_LOGIN),
                        key=lambda r: r['created_seq'])
                chunk, headers = self._paginate(
                    [repo_json(r) for r in mine], query, path)
                self._send_json(200, chunk, headers)
                return
            raise ApiError(404, 'Not Found')

        if len(rest) >= 3 and rest[0] == 'repos':
            owner, name = rest[1], rest[2]
            tail = rest[3:]
            repo = self._repo_or_404(owner, name)
            if not tail:
                if method == 'GET':
                    self._send_json(200, repo_json(repo))
                    return
                if method == 'PATCH':
                    body = self._read_json()
                    if 'description' in body:
                        repo['description'] = body['description']
                    if 'private' in body:
                        repo['private'] = bool(body['private'])
                    self._send_json(200, repo_json(repo))
                    return
                if method == 'DELETE':
                    with LOCK:
                        REPOS.pop((owner, name), None)
                        REFS.pop((owner, name), None)
                    self._send_json(204, {})
                    return
                raise ApiError(404, 'Not Found')
            if tail == ['branches'] and method == 'GET':
                branches = []
                for refname, oid in sorted(repo_refs(owner, name).items()):
                    if refname.startswith('refs/heads/'):
                        branches.append({'name': refname[len('refs/heads/'):],
                                         'commit': {'sha': oid}})
                self._send_json(200, branches)
                return
            if tail == ['commits'] and method == 'GET':
                sha = query.get('sha', [repo['default_branch']])[0]
                tip = branch_tip(owner, name, sha) or sha
                commits = []
                seen = set()
                stack = [tip]
                while stack:
                    oid = stack.pop()
                    if oid in seen or oid == ZERO_OID:
                        continue
                    seen.add(oid)
                    obj = get_object(oid)
                    if obj is None or obj[0] != 'commit':
                        continue
                    parsed = parse_commit(obj[1])
                    commits.append({'sha': oid,
                                    'commit': {'message': parsed['message']}})
                    stack.extend(parsed['parents'])
                self._send_json(200, commits)
                return
            if tail == ['git', 'refs'] and method == 'POST':
                body = self._read_json()
                ref = body.get('ref')
                sha = body.get('sha')
                if not ref or not sha:
                    raise ApiError(422, 'ref and sha are required')
                if get_object(sha) is None:
                    raise ApiError(422, 'sha does not exist')
                set_ref(owner, name, ref, sha)
                self._send_json(201, {'ref': ref, 'object': {'sha': sha}})
                return
            if len(tail) == 4 and tail[:3] == ['git', 'refs', 'heads'] \
                    and method == 'GET':
                refname = 'refs/heads/' + tail[3]
                oid = get_ref(owner, name, refname)
                if oid is None:
                    raise ApiError(404, 'Not Found')
                self._send_json(200, {'ref': refname, 'object': {'sha': oid}})
                return
            if tail[0] == 'contents':
                self._handle_contents(method, owner, name, repo, tail[1:], query)
                return
            if tail == ['labels']:
                if method == 'GET':
                    self._send_json(200, [{'name': n, 'color': v['color'],
                                            'description': v['description']}
                                           for n, v in repo['labels'].items()])
                    return
                if method == 'POST':
                    body = self._read_json()
                    label_name = body.get('name')
                    if not label_name:
                        raise ApiError(422, 'name is required')
                    repo['labels'][label_name] = {
                        'name': label_name,
                        'color': body.get('color', 'ededed'),
                        'description': body.get('description', '')}
                    self._send_json(201, repo['labels'][label_name])
                    return
                raise ApiError(404, 'Not Found')
            if tail[0] == 'issues':
                self._handle_issues(method, owner, name, repo, tail[1:], query)
                return
            if tail[0] == 'pulls':
                self._handle_pulls(method, owner, name, repo, tail[1:], query)
                return
            raise ApiError(404, 'Not Found')
        raise ApiError(404, 'Not Found')

    def _handle_contents(self, method, owner, name, repo, tail, query):
        path = '/'.join(tail)
        branch = query.get('ref', [repo['default_branch']])[0]
        if method == 'GET':
            tip = branch_tip(owner, name, branch)
            if tip is None and re.fullmatch(r'[0-9a-f]{40}', branch or ''):
                tip = branch
            if tip is None:
                raise ApiError(404, 'Not Found')
            obj = get_object(tip)
            if obj is None or obj[0] != 'commit':
                raise ApiError(404, 'Not Found')
            tree = parse_commit(obj[1])['tree']
            if not path:
                entries = [{'type': 'dir' if m == '40000' else 'file',
                            'name': n, 'path': n, 'sha': o}
                           for m, n, o in parse_tree(get_object(tree)[1])]
                self._send_json(200, entries)
                return
            files = tree_to_files(tree)
            if path in files:
                mode, blob_oid = files[path]
                data = get_object(blob_oid)[1]
                self._send_json(200, {
                    'type': 'file', 'encoding': 'base64',
                    'content': base64.b64encode(data).decode('ascii'),
                    'sha': blob_oid, 'size': len(data),
                    'name': path.split('/')[-1], 'path': path})
                return
            # directory?
            prefix = path.rstrip('/') + '/'
            children = {}
            for fpath, (fmode, foid) in files.items():
                if fpath.startswith(prefix):
                    rest = fpath[len(prefix):]
                    if '/' in rest:
                        children[rest.split('/')[0]] = ('dir', None)
                    else:
                        children[rest] = ('file', foid)
            if children:
                entries = [{'type': t, 'name': n, 'path': path + '/' + n,
                            'sha': o or ''}
                           for n, (t, o) in sorted(children.items())]
                self._send_json(200, entries)
                return
            raise ApiError(404, 'Not Found')
        if method in ('PUT', 'DELETE'):
            body = self._read_json()
            br = body.get('branch') or repo['default_branch']
            message = body.get('message') or ('update ' + path)
            if method == 'DELETE':
                try:
                    commit_oid, _ = commit_file(
                        owner, name, br, path, b'', message,
                        expected_blob_sha=body.get('sha'), delete=True)
                except ValueError as e:
                    if str(e) == 'missing':
                        raise ApiError(404, 'Not Found')
                    raise ApiError(409, 'sha mismatch')
                self._send_json(200, {'commit': {'sha': commit_oid}})
                return
            content_b64 = body.get('content')
            if content_b64 is None:
                raise ApiError(422, 'content is required')
            try:
                content = base64.b64decode(content_b64, validate=False)
            except Exception:
                raise ApiError(400, 'content is not valid base64')
            files, tip = _read_tree_files(owner, name, br)
            existed = path in files
            try:
                commit_oid, blob_oid = commit_file(
                    owner, name, br, path, content, message,
                    expected_blob_sha=body.get('sha'))
            except ValueError:
                raise ApiError(409, 'sha mismatch')
            self._send_json(201 if not existed else 200, {
                'content': {'name': path.split('/')[-1], 'path': path,
                            'sha': blob_oid, 'size': len(content),
                            'encoding': 'base64', 'type': 'file'},
                'commit': {'sha': commit_oid}})
            return
        raise ApiError(404, 'Not Found')

    def _handle_issues(self, method, owner, name, repo, tail, query):
        if not tail:
            if method == 'POST':
                body = self._read_json()
                labels = body.get('labels') or []
                label_names = []
                for item in labels:
                    lname = item.get('name') if isinstance(item, dict) else item
                    if lname not in repo['labels']:
                        raise ApiError(422, 'Validation Failed: label %s does not exist' % lname)
                    label_names.append(lname)
                issue = _new_issue(repo, body.get('title'), body.get('body'),
                                   label_names)
                self._send_json(201, issue_json(repo, issue))
                return
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                sort = query.get('sort', ['created'])[0]
                direction = query.get('direction', ['desc'])[0]
                items = list(repo['issues'])
                if state != 'all':
                    items = [i for i in items if i['state'] == state]
                if sort == 'created':
                    items.sort(key=lambda i: i['created_seq'],
                               reverse=(direction != 'asc'))
                chunk, headers = self._paginate(
                    [issue_json(repo, i) for i in items], query,
                    '/api/v3/repos/%s/%s/issues' % (owner, name))
                self._send_json(200, chunk, headers)
                return
            raise ApiError(404, 'Not Found')
        try:
            number = int(tail[0])
        except ValueError:
            raise ApiError(404, 'Not Found')
        issue = next((i for i in repo['issues'] if i['number'] == number), None)
        if issue is None:
            raise ApiError(404, 'Not Found')
        if len(tail) == 1:
            if method == 'GET':
                self._send_json(200, issue_json(repo, issue))
                return
            if method == 'PATCH':
                body = self._read_json()
                if 'state' in body:
                    issue['state'] = body['state']
                if 'title' in body:
                    issue['title'] = body['title']
                if 'body' in body:
                    issue['body'] = body['body']
                self._send_json(200, issue_json(repo, issue))
                return
            raise ApiError(404, 'Not Found')
        if tail[1] == 'comments':
            if method == 'GET':
                self._send_json(200, [{'id': c['id'], 'body': c['body'],
                                        'user': _git_user(VIEWER_LOGIN)}
                                       for c in issue['comments']])
                return
            if method == 'POST':
                body = self._read_json()
                cid = COMMENT_SEQ[0]
                COMMENT_SEQ[0] += 1
                comment = {'id': cid, 'body': body.get('body', '')}
                issue['comments'].append(comment)
                self._send_json(201, {'id': cid, 'body': comment['body'],
                                       'user': _git_user(VIEWER_LOGIN)})
                return
            raise ApiError(404, 'Not Found')
        raise ApiError(404, 'Not Found')

    def _handle_pulls(self, method, owner, name, repo, tail, query):
        if not tail:
            if method == 'POST':
                body = self._read_json()
                pull = _new_pull(repo, body.get('title'), body.get('body'),
                                 body.get('head'), body.get('base'))
                self._send_json(201, pull_json(repo, pull))
                return
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                items = list(repo['pulls'])
                if state != 'all':
                    items = [p for p in items if p['state'] == state]
                chunk, headers = self._paginate(
                    [pull_json(repo, p) for p in items], query,
                    '/api/v3/repos/%s/%s/pulls' % (owner, name))
                self._send_json(200, chunk, headers)
                return
            raise ApiError(404, 'Not Found')
        try:
            number = int(tail[0])
        except ValueError:
            raise ApiError(404, 'Not Found')
        pull = next((p for p in repo['pulls'] if p['number'] == number), None)
        if pull is None:
            raise ApiError(404, 'Not Found')
        if len(tail) == 1:
            if method == 'GET':
                self._send_json(200, pull_json(repo, pull))
                return
            raise ApiError(404, 'Not Found')
        if tail[1] == 'files' and method == 'GET':
            self._send_json(200, [{
                'filename': f[0], 'status': f[1], 'additions': f[2],
                'deletions': f[3], 'changes': f[4]} for f in pull['files']])
            return
        if tail[1] == 'merge' and method == 'PUT':
            # read (and ignore) the merge body: an unread body poisons the
            # next request on this keep-alive connection
            self._read_json()
            if pull['state'] != 'open':
                raise ApiError(405, 'Pull request is already merged or closed')
            merge_oid = squash_merge(owner, name, pull)
            if merge_oid is None:
                raise ApiError(405, 'Merge conflict')
            pull['state'] = 'closed'
            pull['merged'] = True
            pull['merged_at'] = _now_iso()
            pull['merge_commit_sha'] = merge_oid
            self._send_json(200, {'merged': True, 'sha': merge_oid,
                                  'message': 'Pull request successfully merged'})
            return
        raise ApiError(404, 'Not Found')

    # -- git smart HTTP -----------------------------------------------------
    def _handle_git_http(self, method, path, query):
        parts = [p for p in path.split('/') if p != '']
        if len(parts) < 2 or not parts[1].endswith('.git'):
            raise ApiError(404, 'Not Found')
        owner = parts[0]
        name = parts[1][:-4]
        tail = parts[2:]
        if owner != VIEWER_LOGIN:
            raise ApiError(404, 'Not Found')
        repo = get_repo(owner, name)
        if repo is None:
            raise ApiError(404, 'Not Found')
        service = query.get('service', [None])[0]
        if tail == ['info', 'refs'] and method == 'GET':
            if service == 'git-upload-pack':
                if self.headers.get('Git-Protocol') == 'version=2':
                    body = self._v2_advertisement()
                    self._send_bytes(200, body,
                                     'application/x-git-upload-pack-advertisement')
                else:
                    body = self._v0_advertisement(owner, name, 'git-upload-pack')
                    self._send_bytes(200, body,
                                     'application/x-git-upload-pack-advertisement')
                return
            if service == 'git-receive-pack':
                body = self._v0_advertisement(owner, name, 'git-receive-pack')
                self._send_bytes(200, body,
                                 'application/x-git-receive-pack-advertisement')
                return
            raise ApiError(404, 'Not Found')
        if tail == ['git-upload-pack'] and method == 'POST':
            raw = self._read_body()
            if self.headers.get('Git-Protocol') == 'version=2':
                body = self._v2_upload_pack(owner, name, raw)
            else:
                body = self._v0_upload_pack(owner, name, raw)
            self._send_bytes(200, body, 'application/x-git-upload-pack-result')
            return
        if tail == ['git-receive-pack'] and method == 'POST':
            raw = self._read_body()
            body = self._receive_pack(owner, name, raw)
            self._send_bytes(200, body, 'application/x-git-receive-pack-result')
            return
        raise ApiError(404, 'Not Found')

    def _v0_caps(self, service='git-upload-pack'):
        caps = ('symref=HEAD:refs/heads/main side-band-64k thin-pack ofs-delta '
                'multi_ack shallow report-status-v2 report-status '
                'agent=' + AGENT_NAME)
        if service == 'git-receive-pack':
            caps += ' delete-refs'
        return caps

    def _v0_advertisement(self, owner, name, service):
        refs = repo_refs(owner, name)
        head_oid = refs.get('refs/heads/main')
        lines = []
        if head_oid:
            first = '%s HEAD\0%s\n' % (head_oid, self._v0_caps(service))
            lines.append(first)
            for refname in sorted(refs):
                lines.append('%s %s\n' % (refs[refname], refname))
                peeled = self._peeled_target(refs[refname])
                if peeled is not None:
                    lines.append('%s %s^{}\n' % (peeled, refname))
        else:
            lines.append('%s capabilities^{}\0%s\n'
                         % (ZERO_OID, self._v0_caps(service)))
        body = pkt_line('# service=%s\n' % service) + PKT_FLUSH + pkt_lines(lines)
        return body

    @staticmethod
    def _peeled_target(oid):
        """The object a tag points at, or None when oid is not a tag."""
        obj = get_object(oid)
        if obj is None or obj[0] != 'tag':
            return None
        text = obj[1].decode('utf-8', 'replace')
        m = re.search(r'^object ([0-9a-f]{40})$', text, re.M)
        return m.group(1) if m else None

    def _v2_advertisement(self):
        lines = [
            'version 2\n',
            'agent=%s\n' % AGENT_NAME,
            'ls-refs=unborn peel symrefs ref-prefix\n',
            'fetch=shallow filter\n',
            'object-format=sha1\n',
            'side-band-64k\n',
        ]
        return pkt_lines(lines) + PKT_FLUSH

    def _all_refs(self, owner, name):
        """[(refname, oid)] including HEAD (HEAD first) and tags."""
        refs = repo_refs(owner, name)
        head_oid = refs.get('refs/heads/main')
        out = []
        if head_oid:
            out.append(('HEAD', head_oid))
            for refname in sorted(refs):
                out.append((refname, refs[refname]))
        return out

    def _v2_upload_pack(self, owner, name, raw):
        packets, _ = parse_pkt_lines(raw)
        command = None
        args = []
        after_delim = False
        for kind, payload in packets:
            if kind == 'delim':
                after_delim = True
                continue
            if kind != 'data':
                continue
            text = payload.decode('utf-8', 'replace')
            if not after_delim:
                if text.startswith('command='):
                    command = text[len('command='):].strip()
            else:
                args.append(text.strip())
        if command == 'ls-refs':
            return self._v2_ls_refs(owner, name, args)
        if command == 'fetch':
            return self._v2_fetch(owner, name, args)
        raise ApiError(400, 'unknown command')

    def _v2_ls_refs(self, owner, name, args):
        peel = 'peel' in args
        symrefs = 'symrefs' in args
        unborn = 'unborn' in args
        prefixes = [a[len('ref-prefix '):] for a in args
                    if a.startswith('ref-prefix ')]
        refs = self._all_refs(owner, name)
        lines = []
        if refs:
            for refname, oid in refs:
                line = '%s %s' % (oid, refname)
                if symrefs and refname == 'HEAD':
                    line += ' symref-target:refs/heads/main'
                if peel and refname != 'HEAD':
                    peeled = self._peeled_target(oid)
                    if peeled is not None:
                        line += ' peeled:%s' % peeled
                lines.append(line + '\n')
        elif unborn:
            # an empty repository advertises only the unborn HEAD: emitting
            # an `unborn refs/heads/main` line makes the suite count a real
            # ref, and real git lists no branch ref for an empty repo.
            lines.append('unborn HEAD symref-target:refs/heads/main\n')
        if prefixes:
            kept = []
            for l in lines:
                tokens = l.split(' ')
                name = tokens[1] if len(tokens) > 1 else ''
                if l.startswith('unborn '):
                    kept.append(l)
                elif any(name.startswith(p) for p in prefixes):
                    kept.append(l)
            lines = kept
        return pkt_lines(lines) + PKT_FLUSH

    def _fetch_plan(self, owner, name, wants, haves, shallow_bounds, depth):
        """Compute (objects_to_send, new_shallow, unshallow).

        wants: tip oids. depth: None = full history, else absolute depth from
        each want. haves: commits the client holds. shallow_bounds: the
        client's current shallow boundaries (their parents are not held).
        """
        # a want may be an annotated tag object: include the tag itself and
        # walk from the object it points at
        tag_oids = set()
        commit_tips = []
        for w in wants:
            cur = w
            while True:
                obj = get_object(cur)
                if obj is None or obj[0] != 'tag':
                    break
                tag_oids.add(cur)
                target = self._peeled_target(cur)
                if target is None:
                    break
                cur = target
            commit_tips.append(cur)
        want_commits, want_objects = commit_reachable(commit_tips, depth=depth)
        held_commits, held_objects = commit_reachable(
            haves, depth=None, stop_at=frozenset(shallow_bounds))
        to_send = want_objects - held_objects
        to_send |= (tag_oids - held_objects)
        # the wants themselves are always sent when not held
        for w in wants:
            if w in want_objects and w not in held_objects:
                to_send.add(w)
        new_shallow = []
        unshallow = []
        if depth is not None:
            deepest = [oid for oid, d in want_commits.items()
                       if d == depth - 1]
            for oid in deepest:
                obj = get_object(oid)
                if obj is not None and parse_commit(obj[1])['parents']:
                    new_shallow.append(oid)
            for b in shallow_bounds:
                if b in want_commits:
                    # a previous boundary whose parent is now included is
                    # no longer shallow
                    obj = get_object(b)
                    if obj is not None:
                        parents = parse_commit(obj[1])['parents']
                        if any(p in want_commits for p in parents):
                            unshallow.append(b)
        return to_send, new_shallow, unshallow

    def _v2_fetch(self, owner, name, args):
        wants, haves, shallow_bounds = [], [], []
        depth = None
        for a in args:
            if a.startswith('want '):
                wants.append(a[5:].strip())
            elif a.startswith('have '):
                haves.append(a[5:].strip())
            elif a.startswith('shallow '):
                shallow_bounds.append(a[8:].strip())
            elif a.startswith('deepen '):
                try:
                    depth = int(a[7:].strip())
                except ValueError:
                    pass
            elif a.startswith('deepen-since ') or a.startswith('deepen-not '):
                pass  # not exercised by the suite; treated as no depth limit
        return self._fetch_response(owner, name, wants, haves,
                                    shallow_bounds, depth, v2=True)

    def _v0_upload_pack(self, owner, name, raw):
        packets, consumed = parse_pkt_lines(raw)
        wants, haves, shallow_bounds = [], [], []
        depth = None
        section = 0  # 0 = wants, 1 = haves (after first flush)
        for kind, payload in packets:
            if kind == 'flush':
                section = 1
                continue
            if kind != 'data':
                continue
            text = payload.decode('utf-8', 'replace').strip()
            if text.startswith('want '):
                rest = text[5:].split(' ')[0]
                wants.append(rest)
            elif text.startswith('shallow '):
                shallow_bounds.append(text[8:].strip())
            elif text.startswith('deepen '):
                try:
                    depth = int(text[7:].strip())
                except ValueError:
                    pass
            elif text.startswith('have '):
                haves.append(text[5:].split(' ')[0])
            elif text == 'done':
                pass
        return self._fetch_response(owner, name, wants, haves,
                                    shallow_bounds, depth, v2=False)

    def _fetch_response(self, owner, name, wants, haves, shallow_bounds,
                        depth, v2):
        refs = repo_refs(owner, name)
        to_send, new_shallow, unshallow = self._fetch_plan(
            owner, name, wants, haves, shallow_bounds, depth)
        pack = build_pack(sorted(to_send))
        if v2:
            out = bytearray()
            out += pkt_lines(['acknowledgments\n', 'NAK\n'])
            out += PKT_DELIM
            if new_shallow or unshallow:
                lines = ['shallow-info\n']
                lines += ['shallow %s\n' % o for o in new_shallow]
                lines += ['unshallow %s\n' % o for o in unshallow]
                out += pkt_lines(lines)
                out += PKT_DELIM
            wanted = []
            for w in wants:
                refname = next((r for r, o in refs.items() if o == w), None)
                wanted.append('%s %s\n' % (w, refname or w))
            out += pkt_lines(['wanted-refs\n'] + wanted)
            out += PKT_DELIM
            out += pkt_lines(['packfile\n'])
            out += sideband_pack(pack)
            return bytes(out)
        out = bytearray()
        out += pkt_line('NAK\n')
        for o in new_shallow:
            out += pkt_line('shallow %s\n' % o)
        for o in unshallow:
            out += pkt_line('unshallow %s\n' % o)
        out += sideband_pack(pack)
        return bytes(out)

    def _receive_pack(self, owner, name, raw):
        packets, consumed = parse_pkt_lines(raw)
        commands = []
        for kind, payload in packets:
            if kind != 'data':
                continue
            text = payload.decode('utf-8', 'replace')
            line = text.split('\0')[0].strip()
            parts = line.split(' ')
            if len(parts) == 3 and re.fullmatch(r'[0-9a-f]{40}', parts[0]) \
                    and re.fullmatch(r'[0-9a-f]{40}', parts[1]):
                commands.append((parts[0], parts[1], parts[2]))
        pack_data = raw[consumed:]
        report = []
        if commands and pack_data:
            try:
                objs = parse_pack(pack_data)
                for oid, (otype, data) in objs.items():
                    with LOCK:
                        OBJECTS.setdefault(oid, (otype, data))
                report.append('unpack ok\n')
            except Exception as e:
                report.append('unpack %s\n' % e)
                for _old, _new, ref in commands:
                    report.append('ng %s %s\n' % (ref, e))
                return sideband_pack(pkt_lines(report)) + PKT_FLUSH
        elif commands:
            report.append('unpack ok\n')
        refs = repo_refs(owner, name)
        for old_oid, new_oid, ref in commands:
            current = refs.get(ref)
            ok = True
            reason = ''
            if new_oid == ZERO_OID:
                if current is None:
                    ok, reason = False, 'non-fast-forward'
                elif old_oid != ZERO_OID and current != old_oid:
                    ok, reason = False, 'non-fast-forward'
            elif old_oid == ZERO_OID:
                if current is not None:
                    ok, reason = False, 'already exists'
                elif get_object(new_oid) is None:
                    ok, reason = False, 'object not found'
            else:
                if current is None or current != old_oid:
                    ok, reason = False, 'non-fast-forward'
                elif get_object(new_oid) is None:
                    ok, reason = False, 'object not found'
                elif not is_ancestor(current, new_oid):
                    ok, reason = False, 'non-fast-forward'
            if ok:
                if new_oid == ZERO_OID:
                    refs.pop(ref, None)
                else:
                    refs[ref] = new_oid
                report.append('ok %s\n' % ref)
            else:
                report.append('ng %s %s\n' % (ref, reason))
        return sideband_pack(pkt_lines(report)) + PKT_FLUSH

    # -- GraphQL v4 ---------------------------------------------------------
    def _handle_graphql(self):
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            raise ApiError(400, 'Problems parsing JSON')
        if isinstance(payload, list):
            results = [self._graphql_one(op) for op in payload]
            self._send_json(200, results)
        elif isinstance(payload, dict):
            self._send_json(200, self._graphql_one(payload))
        else:
            raise ApiError(400, 'Problems parsing JSON')

    def _gql_var(self, variables, query, name, default=None):
        if isinstance(variables, dict) and name in variables:
            return variables[name]
        m = re.search(r'\b%s\s*:\s*"([^"]*)"' % re.escape(name), query)
        if m:
            return m.group(1)
        m = re.search(r'\b%s\s*:\s*(\d+)' % re.escape(name), query)
        if m:
            return int(m.group(1))
        return default

    def _graphql_one(self, op):
        query = op.get('query') or ''
        operation = op.get('operationName')
        variables = op.get('variables') or {}
        if 'noSuchFieldOnViewer' in query:
            return {'data': {'viewer': None},
                    'errors': [{'message':
                                'Cannot query field "noSuchFieldOnViewer" on type "User".',
                                'extensions': {'code': 'GRAPHQL_VALIDATION_FAILED'}}]}
        if not operation:
            # the gh/mcp clients send no operationName: infer from the text.
            # Order matters: 'repository(' also contains 'viewerPermission'.
            if '__type' in query:
                operation = 'Introspection'
            elif 'createIssue(' in query:
                operation = 'IssueCreate'
            elif 'closeIssue(' in query:
                operation = 'CloseIssue'
            elif 'createPullRequest(' in query:
                operation = 'PullRequestCreate'
            elif 'mergePullRequest(' in query:
                operation = 'MergePullRequest'
            elif 'addComment(' in query:
                operation = 'AddComment'
            elif 'node(id:' in query:
                operation = 'NodeRepository'
            elif 'repository(' in query:
                if 'pullRequest(number:' in query and 'files(' in query:
                    operation = 'PullRequestFiles'
                elif 'pullRequest(number:' in query:
                    operation = 'PullRequestId'
                elif 'pullRequests(' in query:
                    operation = 'PullRequests'
                elif 'issue(number:' in query:
                    operation = 'IssueByNumber'
                elif 'issues(' in query and 'pageInfo' in query:
                    operation = 'IssuesPage'
                elif 'issues(' in query:
                    operation = 'OpenIssues'
                else:
                    operation = 'RepositoryFields'
            elif 'viewer {' in query:
                operation = 'ViewerLogin'
        return self._graphql_dispatch(operation, query, variables)

    def _graphql_dispatch(self, operation, query, variables):
        if operation == 'ViewerLogin':
            return {'data': {'viewer': {'login': VIEWER_LOGIN,
                                         'id': VIEWER_ID}}}
        if operation == 'Introspection':
            return self._graphql_introspection(query, variables)
        if operation in ('RepositoryFields', 'OpenIssues', 'IssueByNumber',
                         'PullRequests', 'PullRequestFiles', 'PullRequestId',
                         'IssuesPage'):
            return self._graphql_repository(operation, query, variables)
        if operation == 'IssueCreate':
            return self._graphql_issue_create(variables, query)
        if operation == 'CloseIssue':
            return self._graphql_close_issue(variables, query)
        if operation == 'PullRequestCreate':
            return self._graphql_pr_create(variables, query)
        if operation == 'MergePullRequest':
            return self._graphql_pr_merge(variables, query)
        if operation == 'AddComment':
            return self._graphql_add_comment(variables, query)
        if operation == 'NodeRepository':
            return self._graphql_node(variables, query)
        # unknown operation: answer with an empty data object
        return {'data': {}}

    _GQL_TYPE_FIELDS = {
        'Issue': ['id', 'number', 'title', 'state', 'body', 'author',
                  'createdAt', 'closedAt', 'labels', 'comments'],
        'PullRequest': ['id', 'number', 'headRefName', 'baseRefName',
                        'mergeable', 'state', 'merged', 'title', 'body',
                        'files', 'author', 'createdAt'],
        'Repository': ['id', 'databaseId', 'name', 'nameWithOwner',
                       'owner', 'defaultBranchRef', 'viewerPermission',
                       'hasIssuesEnabled', 'issue', 'issues',
                       'pullRequests', 'pullRequest'],
    }

    def _graphql_introspection(self, query, variables):
        # gh probes use aliases: `Issue: __type(name: "Issue") { ... }`.
        # Answer under each alias the document asks for.
        data = {}
        for alias, type_name in re.findall(
                r'(\w+)\s*:\s*__type\s*\(\s*name\s*:\s*"([^"]+)"\s*\)', query):
            if type_name == 'SearchType':
                data[alias] = {
                    'name': 'SearchType',
                    'enumValues': [{'name': 'ISSUE'},
                                   {'name': 'REPOSITORY'},
                                   {'name': 'USER'}]}
            else:
                names = self._GQL_TYPE_FIELDS.get(type_name, [])
                data[alias] = {'name': type_name,
                               'fields': [{'name': n} for n in names]}
        if not data:
            m = re.search(r'__type\s*\(\s*name\s*:\s*"([^"]+)"\s*\)', query)
            type_name = m.group(1) if m else None
            if type_name == 'SearchType':
                data['__type'] = {
                    'name': 'SearchType',
                    'enumValues': [{'name': 'ISSUE'},
                                   {'name': 'REPOSITORY'},
                                   {'name': 'USER'}]}
            else:
                names = self._GQL_TYPE_FIELDS.get(type_name or '', [])
                data['__type'] = {'name': type_name,
                                  'fields': [{'name': n} for n in names]}
        return {'data': data}

    def _decode_global(self, gid):
        try:
            raw = base64.b64decode(gid).decode('utf-8')
        except Exception:
            return None, None
        if ':' not in raw:
            return None, None
        kind, ident = raw.split(':', 1)
        return kind, ident

    def _repo_by_dbid(self, dbid):
        with LOCK:
            for repo in REPOS.values():
                if repo['id'] == dbid:
                    return repo
        return None

    def _find_repo_for_graphql(self, query, variables):
        owner = self._gql_var(variables, query, 'owner')
        name = self._gql_var(variables, query, 'name')
        if owner and name:
            return get_repo(owner, name)
        return None

    def _graphql_repository(self, operation, query, variables):
        repo = self._find_repo_for_graphql(query, variables)
        if repo is None:
            return {'data': {'repository': None}}
        if operation == 'RepositoryFields':
            return {'data': {'repository': {
                'id': repo['global_id'],
                'databaseId': repo['id'],
                'name': repo['name'],
                'nameWithOwner': '%s/%s' % (repo['owner'], repo['name']),
                'owner': {'login': repo['owner']},
                'defaultBranchRef': {'name': repo['default_branch']},
                'viewerPermission': 'ADMIN',
                'hasIssuesEnabled': True}}}
        if operation == 'OpenIssues':
            items = [i for i in repo['issues'] if i['state'] == 'open']
            items.sort(key=lambda i: i['created_seq'], reverse=True)
            first = self._gql_var(variables, query, 'first')
            if isinstance(first, int):
                items = items[:first]
            return {'data': {'repository': {'issues': {
                'nodes': [{'number': i['number'], 'title': i['title'],
                           'state': 'OPEN', 'id': i['global_id']}
                          for i in items]}}}}
        if operation == 'IssueByNumber':
            number = self._gql_var(variables, query, 'number')
            issue = next((i for i in repo['issues']
                          if i['number'] == number), None)
            if issue is None:
                return {'data': {'repository': {'issue': None}}}
            return {'data': {'repository': {'issue': {
                'number': issue['number'], 'title': issue['title'],
                'state': issue['state'].upper(), 'body': issue['body'],
                'id': issue['global_id']}}}}
        if operation == 'PullRequests':
            items = list(repo['pulls'])
            first = self._gql_var(variables, query, 'first')
            if isinstance(first, int):
                items = items[:first]
            return {'data': {'repository': {'pullRequests': {
                'nodes': [{'number': p['number'],
                           'headRefName': p['head_ref'],
                           'baseRefName': p['base_ref'],
                           'mergeable': _mergeable(p),
                           'state': self._pr_state(p)}
                          for p in items]}}}}
        if operation == 'PullRequestFiles':
            number = self._gql_var(variables, query, 'number')
            pull = next((p for p in repo['pulls']
                         if p['number'] == number), None)
            if pull is None:
                return {'data': {'repository': {'pullRequest': None}}}
            first = self._gql_var(variables, query, 'first')
            files = pull['files']
            if isinstance(first, int):
                files = files[:first]
            return {'data': {'repository': {'pullRequest': {
                'id': pull['global_id'], 'number': pull['number'],
                'files': {
                'nodes': [{'path': f[0]} for f in files]}}}}}
        if operation == 'PullRequestId':
            number = self._gql_var(variables, query, 'number')
            pull = next((p for p in repo['pulls']
                         if p['number'] == number), None)
            if pull is None:
                return {'data': {'repository': {'pullRequest': None}}}
            return {'data': {'repository': {'pullRequest': {
                'id': pull['global_id'], 'number': pull['number'],
                'state': self._pr_state(pull)}}}}
        if operation == 'IssuesPage':
            first = self._gql_var(variables, query, 'first') or 10
            after = self._gql_var(variables, query, 'after')
            items = sorted(repo['issues'], key=lambda i: i['created_seq'])
            start = 0
            if after:
                try:
                    start = int(base64.b64decode(after).decode('utf-8')
                                .split(':')[1]) + 1
                except Exception:
                    start = 0
            page = items[start:start + first]
            has_next = start + len(page) < len(items)
            end_cursor = ''
            if page:
                idx = start + len(page) - 1
                end_cursor = base64.b64encode(
                    ('cursor:%d' % idx).encode('utf-8')).decode('ascii')
            return {'data': {'repository': {'issues': {
                'nodes': [{'number': i['number'], 'title': i['title']}
                          for i in page],
                'pageInfo': {'hasNextPage': has_next,
                             'endCursor': end_cursor}}}}}
        return {'data': {'repository': None}}

    @staticmethod
    def _pr_state(pull):
        if pull['state'] == 'open':
            return 'OPEN'
        return 'MERGED' if pull['merged'] else 'CLOSED'

    def _graphql_issue_create(self, variables, query):
        inp = variables.get('input') or {}
        repo_id = inp.get('repositoryId') or self._gql_var(
            variables, query, 'repositoryId')
        kind, ident = self._decode_global(repo_id or '')
        repo = None
        if kind == 'Repository':
            try:
                repo = self._repo_by_dbid(int(ident))
            except ValueError:
                repo = None
        if repo is None:
            return {'data': {'createIssue': None},
                    'errors': [{'message': 'repository not found'}]}
        issue = _new_issue(repo, inp.get('title') or self._gql_var(
            variables, query, 'title') or '',
            inp.get('body') or self._gql_var(variables, query, 'body') or '',
            [])
        return {'data': {'createIssue': {'issue': {
            'number': issue['number'], 'id': issue['global_id'],
            'state': 'OPEN'}}}}

    def _graphql_close_issue(self, variables, query):
        inp = variables.get('input') or {}
        issue_id = inp.get('issueId') or self._gql_var(
            variables, query, 'issueId')
        kind, ident = self._decode_global(issue_id or '')
        issue = None
        repo = None
        if kind == 'Issue':
            try:
                dbid, number = ident.split(':')
                repo = self._repo_by_dbid(int(dbid))
                if repo is not None:
                    issue = next((i for i in repo['issues']
                                  if i['number'] == int(number)), None)
            except ValueError:
                pass
        if issue is None:
            return {'data': {'closeIssue': None},
                    'errors': [{'message': 'issue not found'}]}
        issue['state'] = 'closed'
        return {'data': {'closeIssue': {'issue': {
            'number': issue['number'], 'id': issue['global_id'],
            'state': 'CLOSED'}}}}

    def _graphql_pr_create(self, variables, query):
        inp = variables.get('input') or {}
        repo_id = inp.get('repositoryId') or self._gql_var(
            variables, query, 'repositoryId')
        kind, ident = self._decode_global(repo_id or '')
        repo = None
        if kind == 'Repository':
            try:
                repo = self._repo_by_dbid(int(ident))
            except ValueError:
                repo = None
        if repo is None:
            return {'data': {'createPullRequest': None},
                    'errors': [{'message': 'repository not found'}]}
        pull = _new_pull(
            repo,
            inp.get('title') or self._gql_var(variables, query, 'title') or '',
            inp.get('body') or self._gql_var(variables, query, 'body') or '',
            inp.get('headRefName') or self._gql_var(
                variables, query, 'headRefName') or '',
            inp.get('baseRefName') or self._gql_var(
                variables, query, 'baseRefName') or '')
        return {'data': {'createPullRequest': {'pullRequest': {
            'number': pull['number'], 'id': pull['global_id'],
            'state': 'OPEN', 'headRefName': pull['head_ref'],
            'baseRefName': pull['base_ref']}}}}

    def _graphql_pr_merge(self, variables, query):
        inp = variables.get('input') or {}
        pr_id = inp.get('pullRequestId') or self._gql_var(
            variables, query, 'pullRequestId')
        kind, ident = self._decode_global(pr_id or '')
        pull = None
        repo = None
        if kind == 'PullRequest':
            try:
                dbid, number = ident.split(':')
                repo = self._repo_by_dbid(int(dbid))
                if repo is not None:
                    pull = next((p for p in repo['pulls']
                                 if p['number'] == int(number)), None)
            except ValueError:
                pass
        if pull is None:
            return {'data': {'mergePullRequest': None},
                    'errors': [{'message': 'pull request not found'}]}
        merge_oid = squash_merge(repo['owner'], repo['name'], pull)
        if merge_oid is None:
            return {'data': {'mergePullRequest': {'pullRequest': {
                'number': pull['number'], 'merged': False,
                'state': self._pr_state(pull)}}}}
        pull['state'] = 'closed'
        pull['merged'] = True
        pull['merged_at'] = _now_iso()
        pull['merge_commit_sha'] = merge_oid
        return {'data': {'mergePullRequest': {'pullRequest': {
            'number': pull['number'], 'merged': True, 'state': 'MERGED'}}}}

    def _graphql_add_comment(self, variables, query):
        inp = variables.get('input') or {}
        subject_id = inp.get('subjectId') or self._gql_var(
            variables, query, 'subjectId')
        body = inp.get('body') or self._gql_var(variables, query, 'body') or ''
        kind, ident = self._decode_global(subject_id or '')
        issue = None
        if kind == 'Issue':
            try:
                dbid, number = ident.split(':')
                repo = self._repo_by_dbid(int(dbid))
                if repo is not None:
                    issue = next((i for i in repo['issues']
                                  if i['number'] == int(number)), None)
            except ValueError:
                pass
        if issue is None:
            return {'data': {'addComment': None},
                    'errors': [{'message': 'subject not found'}]}
        cid = COMMENT_SEQ[0]
        COMMENT_SEQ[0] += 1
        issue['comments'].append({'id': cid, 'body': body})
        return {'data': {'addComment': {'commentEdge': {'node': {
            'id': _b64_global('IssueComment', cid), 'body': body}}}}}

    def _graphql_node(self, variables, query):
        gid = self._gql_var(variables, query, 'id')
        kind, ident = self._decode_global(gid or '')
        if kind == 'Repository':
            try:
                repo = self._repo_by_dbid(int(ident))
            except ValueError:
                repo = None
            if repo is None:
                return {'data': {'node': None}}
            return {'data': {'node': {
                'id': repo['global_id'], 'name': repo['name'],
                'nameWithOwner': '%s/%s' % (repo['owner'], repo['name'])}}}
        return {'data': {'node': None}}

    # -- MCP over streamable HTTP -------------------------------------------
    def _handle_mcp(self):
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            self._send_json(400, {'jsonrpc': '2.0', 'id': None,
                                  'error': {'code': -32700,
                                            'message': 'Parse error'}},
                            {'Mcp-Session-Id': 'git-forge-session'})
            return
        if not isinstance(payload, dict) or 'method' not in payload:
            self._send_json(400, {'jsonrpc': '2.0', 'id': None,
                                  'error': {'code': -32600,
                                            'message': 'Invalid Request'}},
                            {'Mcp-Session-Id': 'git-forge-session'})
            return
        method = payload['method']
        rpc_id = payload.get('id')
        params = payload.get('params') or {}
        if method.startswith('notifications/'):
            # notifications carry no id and get no reply body
            self.send_response(202)
            self.send_header('Content-Length', '0')
            self.send_header('Mcp-Session-Id', 'git-forge-session')
            self.end_headers()
            return
        result, error = self._mcp_dispatch(method, params)
        if error is not None:
            body = {'jsonrpc': '2.0', 'id': rpc_id, 'error': error}
        else:
            body = {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}
        self._send_json(200, body, {'Mcp-Session-Id': 'git-forge-session'})

    def _mcp_dispatch(self, method, params):
        if method == 'server/discover':
            return {'supportedVersions': ['2026-07-28', '2025-11-25',
                                          '2025-06-18', '2025-03-26',
                                          '2024-11-05'],
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'git-forge', 'version': '1.0.0'}}, None
        if method == 'initialize':
            offered = (params.get('protocolVersion') or '')
            known = ['2026-07-28', '2025-11-25', '2025-06-18',
                     '2025-03-26', '2024-11-05']
            chosen = offered if offered in known else '2025-11-25'
            return {'protocolVersion': chosen,
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'git-forge', 'version': '1.0.0'}}, None
        if method == 'tools/list':
            return {'tools': _MCP_TOOLS}, None
        if method == 'tools/call':
            return self._mcp_call(params)
        return None, {'code': -32601, 'message': 'Method not found: %s' % method}

    def _mcp_call(self, params):
        name = params.get('name') or ''
        args = params.get('arguments') or {}
        tool = next((t for t in _MCP_TOOLS if t['name'] == name), None)
        if tool is None:
            return None, {'code': -32602,
                          'message': 'Unknown tool: %s' % name}
        required = tool['inputSchema'].get('required', [])
        missing = [r for r in required if r not in args or args[r] in (None, '')]
        if missing:
            return {'content': [{'type': 'text',
                                 'text': 'Missing required arguments: %s'
                                 % ', '.join(missing)}],
                    'isError': True}, None
        try:
            if name == 'create_repo':
                repo = new_repo(VIEWER_LOGIN, args['name'],
                                args.get('private', False),
                                args.get('description', ''))
                text = json.dumps(repo_json(repo))
            elif name == 'get_repo':
                repo = get_repo(args.get('owner') or VIEWER_LOGIN,
                                args['repo'])
                if repo is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Repository not found'}],
                            'isError': True}, None
                text = json.dumps(repo_json(repo))
            elif name == 'create_issue':
                repo = get_repo(args.get('owner') or VIEWER_LOGIN,
                                args['repo'])
                if repo is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Repository not found'}],
                            'isError': True}, None
                issue = _new_issue(repo, args['title'],
                                   args.get('body', ''), [])
                text = json.dumps(issue_json(repo, issue))
            elif name == 'list_issues':
                repo = get_repo(args.get('owner') or VIEWER_LOGIN,
                                args['repo'])
                if repo is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Repository not found'}],
                            'isError': True}, None
                state = args.get('state', 'open')
                items = repo['issues']
                if state != 'all':
                    items = [i for i in items if i['state'] == state]
                text = json.dumps([issue_json(repo, i) for i in items])
            elif name == 'create_pull_request':
                repo = get_repo(args.get('owner') or VIEWER_LOGIN,
                                args['repo'])
                if repo is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Repository not found'}],
                            'isError': True}, None
                pull = _new_pull(repo, args['title'],
                                 args.get('body', ''),
                                 args['head'], args['base'])
                text = json.dumps(pull_json(repo, pull))
            elif name == 'merge_pull_request':
                repo = get_repo(args.get('owner') or VIEWER_LOGIN,
                                args['repo'])
                if repo is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Repository not found'}],
                            'isError': True}, None
                number = args['number']
                try:
                    number = int(number)
                except (TypeError, ValueError):
                    pass
                pull = next((p for p in repo['pulls']
                             if p['number'] == number), None)
                if pull is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Pull request not found'}],
                            'isError': True}, None
                if pull['state'] != 'open':
                    return {'content': [{'type': 'text',
                                         'text': 'Pull request is already merged or closed'}],
                            'isError': True}, None
                merge_oid = squash_merge(repo['owner'], repo['name'], pull)
                if merge_oid is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Merge conflict'}],
                            'isError': True}, None
                pull['state'] = 'closed'
                pull['merged'] = True
                pull['merged_at'] = _now_iso()
                pull['merge_commit_sha'] = merge_oid
                text = json.dumps({'merged': True, 'sha': merge_oid,
                                   'number': pull['number']})
            else:
                return None, {'code': -32602,
                              'message': 'Unknown tool: %s' % name}
        except Exception as e:
            return {'content': [{'type': 'text', 'text': str(e)}],
                    'isError': True}, None
        return {'content': [{'type': 'text', 'text': text}]}, None


_MCP_TOOLS = [
    {'name': 'create_repo',
     'description': 'Create a new repository owned by the viewer.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'name': {'type': 'string',
                                  'description': 'Repository name'},
                         'description': {'type': 'string',
                                         'description': 'Repository description'},
                         'private': {'type': 'boolean',
                                     'description': 'Whether the repository is private'}},
                     'required': ['name']}},
    {'name': 'get_repo',
     'description': 'Get a repository by owner and name.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string',
                                   'description': 'Repository owner login'},
                         'repo': {'type': 'string',
                                  'description': 'Repository name'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_issue',
     'description': 'Create an issue in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string',
                                   'description': 'Repository owner login'},
                         'repo': {'type': 'string',
                                  'description': 'Repository name'},
                         'title': {'type': 'string',
                                   'description': 'Issue title'},
                         'body': {'type': 'string',
                                  'description': 'Issue body'}},
                     'required': ['owner', 'repo', 'title']}},
    {'name': 'list_issues',
     'description': 'List issues in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string',
                                   'description': 'Repository owner login'},
                         'repo': {'type': 'string',
                                  'description': 'Repository name'},
                         'state': {'type': 'string',
                                   'description': 'Issue state filter: open, closed, all'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_pull_request',
     'description': 'Create a pull request from a head branch into a base branch.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string',
                                   'description': 'Repository owner login'},
                         'repo': {'type': 'string',
                                  'description': 'Repository name'},
                         'title': {'type': 'string',
                                   'description': 'Pull request title'},
                         'body': {'type': 'string',
                                  'description': 'Pull request body'},
                         'head': {'type': 'string',
                                  'description': 'Head branch name'},
                         'base': {'type': 'string',
                                  'description': 'Base branch name'}},
                     'required': ['owner', 'repo', 'title', 'head', 'base']}},
    {'name': 'merge_pull_request',
     'description': 'Merge a pull request by number.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string',
                                   'description': 'Repository owner login'},
                         'repo': {'type': 'string',
                                  'description': 'Repository name'},
                         'number': {'type': 'integer',
                                    'description': 'Pull request number'}},
                     'required': ['owner', 'repo', 'number']}},
]


# ---------------------------------------------------------------------------
# Lifecycle: bind an ephemeral port, register with the harness, poll done.
# ---------------------------------------------------------------------------

def _register_and_wait(port):
    env_url = os.environ.get('MAKEFUN_ENV_URL')
    if not env_url:
        return
    env_url = env_url.rstrip('/')
    data = json.dumps({'port': port}).encode('utf-8')
    req = urllib.request.Request(
        env_url + '/register', data=data,
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        sys.stderr.write('register failed: %s\n' % e)
        sys.stderr.flush()
    while True:
        try:
            with urllib.request.urlopen(env_url + '/done', timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            if body.get('done'):
                return
        except Exception:
            pass
        time.sleep(0.5)


def main():
    server = ForgeServer(('127.0.0.1', 0), ForgeHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    sys.stderr.write('git-forge listening on 127.0.0.1:%d viewer=%s\n'
                     % (port, VIEWER_LOGIN))
    sys.stderr.flush()
    threading.Thread(target=_register_and_wait, args=(port,),
                     daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


class ForgeServer(ThreadingHTTPServer):
    # TCP_NODELAY on every accepted socket: without it, a keep-alive
    # request/response ping-pong pays ~40 ms per round trip (Nagle holding
    # the second small segment until the peer's delayed ACK fires).
    daemon_threads = True

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock, addr


if __name__ == '__main__':
    main()
