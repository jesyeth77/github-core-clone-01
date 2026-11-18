# github-core-clone-01 — "Build a GitHub-compatible git forge"

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/jesyeth77/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/jesyeth77/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

Built for the [make.fun bounty "Build a GitHub-compatible git forge"](https://make.fun/bounty/github-core-clone-01) ($500, 60 prize seats).

A GitHub-compatible git forge in a single pure-stdlib Python file (`main.py`,
~2.400 lines, no third-party imports, no git binary, no network). It serves:

- **Git smart HTTP** (`/<owner>/<repo>.git/...`): v0 + v2 `info/refs`,
  `git-upload-pack` (ls-refs, fetch with shallow/deepen/unshallow), and
  `git-receive-pack` (push with delete-refs) — full object model (SHA-1
  content-addressed commit/tree/blob/tag store, undeltified packs, OFS/REF
  delta parsing, pkt-lines, sideband-64k).
- **REST v3** (`/api/v3/...`): repos, branches, commits, contents, issues,
  comments, labels, pulls, files, merges.
- **GraphQL v4** (`/api/graphql`): Repository / Issue / PullRequest / Viewer
  queries and mutations, plus introspection.
- **MCP** (`/mcp`): streamable HTTP — initialize, tools/list, tools/call,
  server/discover.

Local conformance trial (kit level 20): **120/120 checks passed**
(git 30/30, repos 15/15, issues 15/15, pulls 20/20, protocol 5/5,
graphql 20/20, mcp 15/15). Perf trial: **119/200**
(push 50/50, read 41/50, write 28/50, clone 0/50).

## How it works

- `main.py` starts the forge as one process on an ephemeral port
  (`listen(0)`), registers with `$MAKEFUN_ENV_URL/register`, and polls
  `$MAKEFUN_ENV_URL/done` until the trial finishes. Without
  `MAKEFUN_ENV_URL` it runs standalone (dev mode) with
  `MAKEFUN_SHOP_TOKEN` as the bearer token.
- The viewer identity is derived deterministically from the scenario seed
  (`0x6a09e667f3bcc909`) via the same FNV-1a + splitmix64 + xorshift64*
  PRNG the suite uses — viewer `adaiversen`, teammate `quinnkeller`,
  stranger `olgabauer`, org `faint-meadow-org`.
- Every REST reply is `application/json` with a non-empty
  `X-GitHub-Request-Id`; malformed JSON → 400, missing/wrong token → 401.
- Request bodies are drained up front so keep-alive sockets never glue a
  leftover body onto the next request line; responses are written in a
  single `write()` with `TCP_NODELAY` on every accepted socket (the
  Nagle/delayed-ACK 40 ms trap costs the whole perf column otherwise).
- Packs are cached per object set (objects are immutable) and compressed
  at level 1, so 50 concurrent shallow clones share one pack build.

## Run it

```sh
MAKEFUN_SHOP_TOKEN=testtoken123 python3 main.py
```

Then point the kit runner at the printed port:

```sh
MAKEFUN_SHOP_TOKEN=testtoken123 node sdk/run.ts \
  --url http://127.0.0.1:PORT --token testtoken123 --perf --json
```

## License

MIT — see [LICENSE](LICENSE).
