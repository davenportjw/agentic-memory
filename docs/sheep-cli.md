# `sheep` CLI & Go Memory Store Reference

`sheep` (`github.com/davenportjw/electric-sheep/pkg/sheep` and `cmd/sheep`) is the standalone demand-driven agent memory CLI, HTTP daemon, and embeddable Go `MemoryStore` implementation for the `toolroll` Agent OS.

- **Runtime**: Pure Go (`go 1.25.0`, `CGO_ENABLED=0`), backed by `modernc.org/sqlite` in Write-Ahead Log (`WAL`) mode (`~/.toolroll/memory.db` by default, or `SHEEP_DB_PATH` / `--db`).
- **Cycle-Free Dependency Graph**: Imports only `github.com/davenportjw/toolroll/utp` (`stdlib`-only) and satisfies `toolroll/pkg/kernel.MemoryStore` structurally.
- **Federated Cloud Spanner Sync**: Optional `/mem/project` + `/mem/org` synchronization maps directly onto the `ScopedMemories` table in `sql/spanner_schema.sql` using Google Cloud Application Default Credentials (`gcloud auth application-default`).

---

## 1. Memory Hierarchy Mounts (`sheep mounts`)

| Mount Prefix | Backend | Mode | Description |
| :--- | :--- | :--- | :--- |
| `/mem/personal` | `sqlite_local` | `rw` | Local-only developer preferences and personal beliefs (`~/.toolroll/memory.db`); never synced off-device. |
| `/mem/project` | `sqlite_repo` / `sqlite_spanner_federated` | `rw` | Repo-scoped crystallized beliefs and symbol notes with optional Cloud Spanner `ScopedMemories` sync. |
| `/mem/org` | `org_policy` | `ro` | Read-only organization security and engineering policy beliefs. |
| `/mem/ext/*` | `http_dark_data` | `ro` | Optional external HTTP dark-data memory mount providers (`GET <endpoint>?repo=<dir>&intent=<query>`). |

---

## 2. CLI Subcommands

### `sheep recall`
Assemble a Turn-0 agent memory frame obeying `utp.ContextMount` byte budgets:
- `clean`: Hard 0-history wall — returns empty output (`0` bytes) and performs zero external mount queries.
- `scoped` *(default)*: Ranked repo/intent crystallized beliefs across `/mem/personal`, `/mem/project`, and `/mem/org`, strictly capped at `<= 2048` bytes (`< 2 KB`).
- `full`: Full relevant crystallized beliefs plus unpromoted session observations, strictly capped at `<= 3800` bytes.

```bash
sheep recall --repo . --intent "WebhookDispatcher retry backoff" --mount scoped
sheep recall --repo . --intent "WebhookDispatcher retry backoff" --mount full --json
sheep recall --repo . --intent "clean critic" --mount clean
```

### `sheep observe`
Record a raw session observation with normalized scope (`/mem/personal`, `/mem/project`, `/mem/org`), `session_id`, AST symbol bindings, and lexical keyword/FTS5 tokens:

```bash
sheep observe --repo . --session sess-01 --scope /mem/project --symbols WebhookDispatcher,RetryPolicy \
  "WebhookDispatcher must honor Retry-After headers on HTTP 429 before exponential backoff"
```

### `sheep note`
Return a `<= 200` byte UTF-8 safe symbol-pinned hazard/memory note for `<Symbol>`, or empty output when none exists:

```bash
sheep note --repo . WebhookDispatcher
```

### `sheep dream`
Run offline Dream Consolidation (`DreamConsolidate`), promoting recurring observations that appear across `min_sessions >= 3` distinct `session_id`s into crystallized beliefs (`scoped_memories` with deterministic confidence score and `support_count`):

```bash
sheep dream --repo .
sheep dream --repo . --json
```

### `sheep evict`
Rewrite symbol bindings across observations and crystallized beliefs when AST symbols are renamed (`OldSym=NewSym`), or evict stale symbol-pinned notes when symbols are deleted (`DeletedSym=`):

```bash
sheep evict --repo . LegacyDispatcher=WebhookDispatcher DeprecatedPoller=
```

### `sheep mounts`
List active `/mem/*` mounts and federation state:

```bash
sheep mounts
sheep mounts --json
```

### `sheep serve`
Start the JSON HTTP server (default `127.0.0.1:7410`):

```bash
sheep serve --addr 127.0.0.1:7410
```

---

## 3. HTTP JSON Endpoints (`sheep serve` & `Store.Handler()`)

| Method & Path | Request Payload / Query | Response JSON |
| :--- | :--- | :--- |
| `GET /healthz` | — | `{"status": "ok", "db_path": "..."}` |
| `GET /api/v1/memory/mounts` | — | `{"mounts": [...]}` |
| `GET\|POST /api/v1/memory/recall` | `{"repo_root": "...", "intent": "...", "mount": "scoped\|full\|clean"}` | `{"frame": "...", "mount": "scoped", "bytes": 142}` |
| `POST /api/v1/memory/observe` | `{"repo_root": "...", "session_id": "...", "scope": "/mem/project", "content": "...", "symbols": ["SymA"]}` *(or header `X-Toolroll-Session-Id`)* | `{"ok": true, "scope": "/mem/project"}` |
| `POST /api/v1/memory/dream` | `{"repo_root": "..."}` | `{"promoted": 1}` |
| `GET\|POST /api/v1/memory/note` | `{"repo_root": "...", "symbol": "SymA"}` | `{"symbol": "SymA", "note": "...", "bytes": 84}` |
| `POST /api/v1/memory/evict` | `{"repo_root": "...", "old_to_new": {"OldSym": "NewSym", "DeadSym": ""}}` | `{"ok": true, "updated": 2}` |

---

## 4. Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `SHEEP_DB_PATH` | `~/.toolroll/memory.db` | Override local SQLite WAL file path. |
| `TOOLROLL_SESSION_ID` | — | Default `--session` identifier for `sheep observe`. |
| `GOOGLE_CLOUD_PROJECT` | `davenport-boutique` | GCP project ID for optional Cloud Spanner `ScopedMemories` sync via ADC. |
| `SHEEP_SPANNER_INSTANCE` | — | Optional Cloud Spanner instance ID enabling `/mem/project` + `/mem/org` sync. |
| `SHEEP_SPANNER_DATABASE` | — | Optional Cloud Spanner database ID matching `sql/spanner_schema.sql`. |
