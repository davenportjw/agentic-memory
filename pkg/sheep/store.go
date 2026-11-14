package sheep

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/davenportjw/electric-sheep/pkg/sheep/loops"
	"github.com/davenportjw/toolroll/utp"
	_ "modernc.org/sqlite"
)

const (
	MaxScopedRecallBytes = 2048
	MaxFullRecallBytes   = 3800
	MaxSymbolNoteBytes   = 200
	DefaultDreamSessions = 3
)

var (
	ErrEmptyDBPath    = errors.New("sheep: empty database path")
	ErrEmptySessionID = errors.New("sheep: empty session_id")
	ErrEmptyContent   = errors.New("sheep: empty observation content")
	ErrInvalidScope   = errors.New("sheep: invalid scope (expected /mem/personal, /mem/project, or /mem/org)")
	ErrInvalidMount   = errors.New("sheep: invalid context mount (expected scoped, full, or clean)")
)

// Config configures a local SQLite WAL sheep.Store and optional federated mounts.
type Config struct {
	DBPath           string
	MinDreamSessions int
	Spanner          *SpannerConfig
}

// Store implements toolroll/pkg/kernel.MemoryStore backed by pure-Go modernc.org/sqlite WAL.
type Store struct {
	mu               sync.RWMutex
	db               *sql.DB
	path             string
	minDreamSessions int
	spanner          *SpannerConfig
	darkMounts       []DarkDataMountConfig
	ftsEnabled       bool
	faultTracker     *loops.PageFaultTracker
	recallInjector   *loops.SymbolRecallInjector
}

// DefaultDBPath returns SHEEP_DB_PATH when set or ~/.toolroll/memory.db.
func DefaultDBPath() string {
	if env := strings.TrimSpace(os.Getenv("SHEEP_DB_PATH")); env != "" {
		return env
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".toolroll", "memory.db")
	}
	return filepath.Join(home, ".toolroll", "memory.db")
}

// Open opens or creates a SQLite WAL memory store at dbPath (or DefaultDBPath() if empty).
func Open(dbPath string) (*Store, error) {
	return OpenWithConfig(Config{DBPath: dbPath})
}

// OpenWithConfig opens or creates a SQLite WAL memory store with explicit configuration.
func OpenWithConfig(cfg Config) (*Store, error) {
	dbPath := strings.TrimSpace(cfg.DBPath)
	if dbPath == "" {
		dbPath = DefaultDBPath()
	}
	if dbPath == "" {
		return nil, ErrEmptyDBPath
	}
	if err := os.MkdirAll(filepath.Dir(dbPath), 0o755); err != nil {
		return nil, fmt.Errorf("sheep: create db dir: %w", err)
	}

	minSessions := cfg.MinDreamSessions
	if minSessions < DefaultDreamSessions {
		minSessions = DefaultDreamSessions
	}

	dsn := fmt.Sprintf("file:%s?_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)", dbPath)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("sheep: open sqlite: %w", err)
	}
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(4)

	s := &Store{
		db:               db,
		path:             dbPath,
		minDreamSessions: minSessions,
		spanner:          cfg.Spanner,
	}
	if err := s.migrate(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

// Path returns the filesystem path of the SQLite WAL database.
func (s *Store) Path() string { return s.path }

// Close closes the underlying SQLite connection pool.
func (s *Store) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

func (s *Store) migrate(ctx context.Context) error {
	const ddl = `
CREATE TABLE IF NOT EXISTS observations (
	observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
	repo_root      TEXT NOT NULL,
	session_id     TEXT NOT NULL,
	scope          TEXT NOT NULL,
	content        TEXT NOT NULL,
	normalized_key TEXT NOT NULL,
	keywords       TEXT NOT NULL DEFAULT '',
	symbols_json   TEXT NOT NULL DEFAULT '[]',
	created_at_ns  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_repo_key ON observations(repo_root, scope, normalized_key);
CREATE INDEX IF NOT EXISTS idx_obs_repo_created ON observations(repo_root, created_at_ns DESC);

CREATE TABLE IF NOT EXISTS observation_symbols (
	observation_id INTEGER NOT NULL REFERENCES observations(observation_id) ON DELETE CASCADE,
	repo_root      TEXT NOT NULL,
	symbol         TEXT NOT NULL,
	PRIMARY KEY (observation_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_obs_sym_lookup ON observation_symbols(repo_root, symbol);

-- ScopedMemories-compatible crystallized beliefs table matching sql/spanner_schema.sql
CREATE TABLE IF NOT EXISTS scoped_memories (
	scope_type          TEXT NOT NULL,
	scope_id            TEXT NOT NULL,
	memory_key          TEXT NOT NULL,
	memory_value        TEXT NOT NULL,
	memory_type         TEXT NOT NULL DEFAULT 'belief',
	category            TEXT NOT NULL DEFAULT 'general',
	confidence          REAL NOT NULL DEFAULT 0.0,
	crystallized_belief TEXT NOT NULL,
	keywords            TEXT NOT NULL DEFAULT '',
	symbols_json        TEXT NOT NULL DEFAULT '[]',
	source_session_id   TEXT NOT NULL DEFAULT '',
	promoted_from_scope TEXT NOT NULL DEFAULT '',
	support_count       INTEGER NOT NULL DEFAULT 1,
	synced_at_ns        INTEGER NOT NULL DEFAULT 0,
	last_updated_ns     INTEGER NOT NULL,
	PRIMARY KEY (scope_type, scope_id, memory_key)
);

CREATE INDEX IF NOT EXISTS idx_scoped_mem_lookup ON scoped_memories(scope_type, scope_id, confidence DESC);

CREATE TABLE IF NOT EXISTS memory_symbols (
	scope_type TEXT NOT NULL,
	scope_id   TEXT NOT NULL,
	memory_key TEXT NOT NULL,
	repo_root  TEXT NOT NULL,
	symbol     TEXT NOT NULL,
	PRIMARY KEY (scope_type, scope_id, memory_key, symbol),
	FOREIGN KEY (scope_type, scope_id, memory_key)
		REFERENCES scoped_memories(scope_type, scope_id, memory_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mem_sym_lookup ON memory_symbols(repo_root, symbol);

CREATE TABLE IF NOT EXISTS lexical_tokens (
	doc_kind  TEXT NOT NULL,
	doc_key   TEXT NOT NULL,
	repo_root TEXT NOT NULL,
	token     TEXT NOT NULL,
	PRIMARY KEY (doc_kind, doc_key, token)
);

CREATE INDEX IF NOT EXISTS idx_lex_token_repo ON lexical_tokens(token, repo_root);

CREATE TABLE IF NOT EXISTS session_delivered_notes (
	session_id      TEXT NOT NULL,
	note_key        TEXT NOT NULL,
	delivered_at_ns INTEGER NOT NULL,
	PRIMARY KEY (session_id, note_key)
);
`
	if _, err := s.db.ExecContext(ctx, ddl); err != nil {
		return fmt.Errorf("sheep: migrate schema: %w", err)
	}

	// Enable SQLite FTS5 virtual table when compiled into the SQLite driver.
	const ftsDDL = `CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(doc_kind, doc_key, repo_root, scope, body);`
	if _, err := s.db.ExecContext(ctx, ftsDDL); err == nil {
		s.ftsEnabled = true
	}
	return nil
}

// NormalizeScope canonicalizes memory scopes to /mem/personal, /mem/project, or /mem/org.
func NormalizeScope(scope string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(scope)) {
	case "", "project", "/mem/project":
		return "/mem/project", nil
	case "personal", "/mem/personal":
		return "/mem/personal", nil
	case "org", "/mem/org":
		return "/mem/org", nil
	default:
		return "", fmt.Errorf("%w: %q", ErrInvalidScope, scope)
	}
}

func scopeToType(normScope string) string {
	switch normScope {
	case "/mem/personal":
		return "personal"
	case "/mem/org":
		return "org"
	default:
		return "project"
	}
}

func normalizeRepoRoot(repoRoot string) string {
	trimmed := strings.TrimSpace(repoRoot)
	if trimmed == "" {
		return "global"
	}
	abs, err := filepath.Abs(trimmed)
	if err != nil {
		return filepath.Clean(trimmed)
	}
	return filepath.Clean(abs)
}

func normalizeSymbols(symbols []string) []string {
	if len(symbols) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(symbols))
	out := make([]string, 0, len(symbols))
	for _, raw := range symbols {
		for _, part := range strings.Split(raw, ",") {
			sym := strings.TrimSpace(part)
			if sym == "" {
				continue
			}
			if _, exists := seen[sym]; !exists {
				seen[sym] = struct{}{}
				out = append(out, sym)
			}
		}
	}
	sort.Strings(out)
	return out
}

func canonicalObservationKey(content string) string {
	var b strings.Builder
	b.Grow(len(content))
	prevSpace := true
	for _, r := range strings.ToLower(strings.TrimSpace(content)) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
			prevSpace = false
		} else if !prevSpace {
			b.WriteByte(' ')
			prevSpace = true
		}
	}
	canon := strings.TrimSpace(b.String())
	sum := sha256.Sum256([]byte(canon))
	return "mem_" + hex.EncodeToString(sum[:8])
}

var stopWords = map[string]struct{}{
	"the": {}, "and": {}, "for": {}, "with": {}, "that": {}, "this": {},
	"from": {}, "into": {}, "when": {}, "must": {}, "always": {}, "never": {},
	"are": {}, "was": {}, "were": {}, "have": {}, "has": {}, "had": {},
	"not": {}, "use": {}, "using": {}, "all": {}, "any": {}, "across": {},
}

// extractLexicalTokens extracts lowercased full identifiers plus CamelCase/snake_case sub-tokens.
func extractLexicalTokens(content string, symbols []string) []string {
	seen := make(map[string]struct{})
	addTok := func(tok string) {
		tok = strings.ToLower(strings.TrimSpace(tok))
		if len(tok) < 2 {
			return
		}
		if _, stop := stopWords[tok]; stop {
			return
		}
		seen[tok] = struct{}{}
	}

	splitIdent := func(ident string) {
		addTok(ident)
		// Split CamelCase and snake_case / kebab-case boundaries.
		runes := []rune(ident)
		start := 0
		for i := 1; i < len(runes); i++ {
			prev := runes[i-1]
			curr := runes[i]
			if !unicode.IsLetter(curr) && !unicode.IsDigit(curr) {
				if start < i {
					addTok(string(runes[start:i]))
				}
				start = i + 1
				continue
			}
			if unicode.IsLower(prev) && unicode.IsUpper(curr) {
				addTok(string(runes[start:i]))
				start = i
			}
		}
		if start < len(runes) {
			addTok(string(runes[start:]))
		}
	}

	var cur strings.Builder
	for _, r := range content {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' || r == '-' {
			cur.WriteRune(r)
		} else if cur.Len() > 0 {
			splitIdent(cur.String())
			cur.Reset()
		}
	}
	if cur.Len() > 0 {
		splitIdent(cur.String())
	}
	for _, sym := range symbols {
		splitIdent(sym)
	}

	out := make([]string, 0, len(seen))
	for tok := range seen {
		out = append(out, tok)
	}
	sort.Strings(out)
	return out
}

func truncateUTF8Bytes(s string, maxBytes int) string {
	if maxBytes <= 0 {
		return ""
	}
	if len(s) <= maxBytes {
		return s
	}
	if maxBytes <= 3 {
		return strings.Repeat(".", maxBytes)
	}
	limit := maxBytes - 3
	for limit > 0 && !utf8.RuneStart(s[limit]) {
		limit--
	}
	trimmed := strings.TrimRightFunc(s[:limit], unicode.IsSpace)
	return trimmed + "..."
}

func indexTokensTx(ctx context.Context, tx *sql.Tx, ftsEnabled bool, docKind, docKey, repoRoot, scope, body string, tokens []string) error {
	if _, err := tx.ExecContext(ctx, `DELETE FROM lexical_tokens WHERE doc_kind = ? AND doc_key = ?`, docKind, docKey); err != nil {
		return err
	}
	for _, tok := range tokens {
		if _, err := tx.ExecContext(ctx,
			`INSERT OR IGNORE INTO lexical_tokens (doc_kind, doc_key, repo_root, token) VALUES (?, ?, ?, ?)`,
			docKind, docKey, repoRoot, tok,
		); err != nil {
			return err
		}
	}
	if ftsEnabled {
		_, _ = tx.ExecContext(ctx, `DELETE FROM memories_fts WHERE doc_kind = ? AND doc_key = ?`, docKind, docKey)
		_, _ = tx.ExecContext(ctx, `INSERT INTO memories_fts (doc_kind, doc_key, repo_root, scope, body) VALUES (?, ?, ?, ?, ?)`,
			docKind, docKey, repoRoot, scope, body)
	}
	return nil
}

// Observe records a raw session observation into SQLite WAL with normalized scope, symbol bindings, and lexical tokens.
func (s *Store) Observe(ctx context.Context, repoRoot, sessionID, scope, content string, symbols []string) error {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return ErrEmptySessionID
	}
	content = strings.TrimSpace(content)
	if content == "" {
		return ErrEmptyContent
	}
	normScope, err := NormalizeScope(scope)
	if err != nil {
		return err
	}

	normRepo := normalizeRepoRoot(repoRoot)
	normSyms := normalizeSymbols(symbols)
	symsJSONBytes, err := json.Marshal(normSyms)
	if err != nil {
		return fmt.Errorf("sheep: marshal symbols: %w", err)
	}
	if normSyms == nil {
		symsJSONBytes = []byte("[]")
	}

	tokens := extractLexicalTokens(content, normSyms)
	keywords := strings.Join(tokens, " ")
	normKey := canonicalObservationKey(content)
	nowNs := time.Now().UTC().UnixNano()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("sheep: begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx, `
		INSERT INTO observations (repo_root, session_id, scope, content, normalized_key, keywords, symbols_json, created_at_ns)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		normRepo, sessionID, normScope, content, normKey, keywords, string(symsJSONBytes), nowNs,
	)
	if err != nil {
		return fmt.Errorf("sheep: insert observation: %w", err)
	}
	obsID, err := res.LastInsertId()
	if err != nil {
		return fmt.Errorf("sheep: observation id: %w", err)
	}

	for _, sym := range normSyms {
		if _, err := tx.ExecContext(ctx, `
			INSERT OR IGNORE INTO observation_symbols (observation_id, repo_root, symbol)
			VALUES (?, ?, ?)`,
			obsID, normRepo, sym,
		); err != nil {
			return fmt.Errorf("sheep: insert observation symbol: %w", err)
		}
	}

	docKey := fmt.Sprintf("obs:%d", obsID)
	if err := indexTokensTx(ctx, tx, s.ftsEnabled, "obs", docKey, normRepo, normScope, content+" "+strings.Join(normSyms, " "), tokens); err != nil {
		return fmt.Errorf("sheep: index observation tokens: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("sheep: commit observation: %w", err)
	}
	return nil
}

// SymbolNote returns a <= 200 byte symbol-pinned memory note when hazards/notes exist for symbol, or "" when none.
func (s *Store) SymbolNote(ctx context.Context, repoRoot, symbol string) (string, error) {
	sym := strings.TrimSpace(symbol)
	if sym == "" {
		return "", nil
	}
	normRepo := normalizeRepoRoot(repoRoot)

	// 1. Prioritize crystallized beliefs in scoped_memories bound to symbol.
	var scopeType, belief string
	var confidence float64
	err := s.db.QueryRowContext(ctx, `
		SELECT sm.scope_type, sm.confidence, sm.crystallized_belief
		FROM scoped_memories sm
		JOIN memory_symbols ms
		  ON sm.scope_type = ms.scope_type
		 AND sm.scope_id = ms.scope_id
		 AND sm.memory_key = ms.memory_key
		WHERE ms.repo_root = ? AND ms.symbol = ?
		ORDER BY sm.confidence DESC, sm.support_count DESC, sm.last_updated_ns DESC
		LIMIT 1`,
		normRepo, sym,
	).Scan(&scopeType, &confidence, &belief)
	if err == nil {
		formatted := fmt.Sprintf("[%s %.2f] %s", scopeType, confidence, belief)
		return truncateUTF8Bytes(formatted, MaxSymbolNoteBytes), nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return "", fmt.Errorf("sheep: query symbol belief: %w", err)
	}

	// 2. Check raw symbol-pinned session observations (excluding Turn-0 [nav] breadcrumbs).
	var rawContent string
	err = s.db.QueryRowContext(ctx, `
		SELECT o.content
		FROM observations o
		JOIN observation_symbols os ON o.observation_id = os.observation_id
		WHERE os.repo_root = ? AND os.symbol = ? AND o.content NOT LIKE '[nav] %'
		ORDER BY o.created_at_ns DESC
		LIMIT 1`,
		normRepo, sym,
	).Scan(&rawContent)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("sheep: query symbol observation: %w", err)
	}
	return truncateUTF8Bytes("[note] "+rawContent, MaxSymbolNoteBytes), nil
}

// DreamConsolidate promotes recurring observations appearing across >= min_sessions (default 3)
// distinct session_ids into crystallized beliefs in scoped_memories.
func (s *Store) DreamConsolidate(ctx context.Context, repoRoot string) (int, error) {
	normRepo := normalizeRepoRoot(repoRoot)

	type candidateGroup struct {
		repoRoot        string
		scope           string
		normKey         string
		distinctSession int
	}

	var query string
	var args []any
	if strings.TrimSpace(repoRoot) == "" {
		query = `
			SELECT repo_root, scope, normalized_key, COUNT(DISTINCT session_id) AS sess_cnt
			FROM observations
			GROUP BY repo_root, scope, normalized_key
			HAVING COUNT(DISTINCT session_id) >= ?`
		args = []any{s.minDreamSessions}
	} else {
		query = `
			SELECT repo_root, scope, normalized_key, COUNT(DISTINCT session_id) AS sess_cnt
			FROM observations
			WHERE repo_root = ?
			GROUP BY repo_root, scope, normalized_key
			HAVING COUNT(DISTINCT session_id) >= ?`
		args = []any{normRepo, s.minDreamSessions}
	}

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, fmt.Errorf("sheep: query consolidation groups: %w", err)
	}
	var groups []candidateGroup
	for rows.Next() {
		var g candidateGroup
		if err := rows.Scan(&g.repoRoot, &g.scope, &g.normKey, &g.distinctSession); err != nil {
			_ = rows.Close()
			return 0, err
		}
		groups = append(groups, g)
	}
	if err := rows.Close(); err != nil {
		return 0, err
	}
	if len(groups) == 0 {
		return 0, nil
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("sheep: begin dream tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	promoted := 0
	nowNs := time.Now().UTC().UnixNano()

	for _, g := range groups {
		scopeType := scopeToType(g.scope)
		scopeID := g.repoRoot

		// Check if already consolidated with >= support_count for idempotency.
		var existingSupport int
		err := tx.QueryRowContext(ctx, `
			SELECT support_count FROM scoped_memories
			WHERE scope_type = ? AND scope_id = ? AND memory_key = ?`,
			scopeType, scopeID, g.normKey,
		).Scan(&existingSupport)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return 0, fmt.Errorf("sheep: check existing belief: %w", err)
		}
		if err == nil && existingSupport >= g.distinctSession {
			continue
		}

		// Gather representative content, latest session_id, and union of symbols across matching observations.
		obsRows, err := tx.QueryContext(ctx, `
			SELECT session_id, content, symbols_json
			FROM observations
			WHERE repo_root = ? AND scope = ? AND normalized_key = ?
			ORDER BY created_at_ns ASC`,
			g.repoRoot, g.scope, g.normKey,
		)
		if err != nil {
			return 0, fmt.Errorf("sheep: load group observations: %w", err)
		}

		var repContent, latestSession string
		var mergedSyms []string
		for obsRows.Next() {
			var sid, content, symsJSON string
			if err := obsRows.Scan(&sid, &content, &symsJSON); err != nil {
				_ = obsRows.Close()
				return 0, err
			}
			if repContent == "" {
				repContent = content
			}
			latestSession = sid
			var syms []string
			if err := json.Unmarshal([]byte(symsJSON), &syms); err == nil {
				mergedSyms = append(mergedSyms, syms...)
			}
		}
		_ = obsRows.Close()

		normSyms := normalizeSymbols(mergedSyms)
		symsBytes, _ := json.Marshal(normSyms)
		if normSyms == nil {
			symsBytes = []byte("[]")
		}
		tokens := extractLexicalTokens(repContent, normSyms)
		keywords := strings.Join(tokens, " ")

		confidence := math.Min(0.99, 0.80+0.05*float64(g.distinctSession-DefaultDreamSessions))
		memType := "belief"
		if len(normSyms) > 0 {
			memType = "symbol_note"
		}
		if scopeType == "org" {
			memType = "policy"
		}

		if _, err := tx.ExecContext(ctx, `
			INSERT INTO scoped_memories (
				scope_type, scope_id, memory_key, memory_value, memory_type, category,
				confidence, crystallized_belief, keywords, symbols_json,
				source_session_id, promoted_from_scope, support_count, last_updated_ns
			) VALUES (?, ?, ?, ?, ?, 'consolidated', ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(scope_type, scope_id, memory_key) DO UPDATE SET
				memory_value = excluded.memory_value,
				memory_type = excluded.memory_type,
				confidence = excluded.confidence,
				crystallized_belief = excluded.crystallized_belief,
				keywords = excluded.keywords,
				symbols_json = excluded.symbols_json,
				source_session_id = excluded.source_session_id,
				support_count = excluded.support_count,
				last_updated_ns = excluded.last_updated_ns`,
			scopeType, scopeID, g.normKey, repContent, memType,
			confidence, repContent, keywords, string(symsBytes),
			latestSession, g.scope, g.distinctSession, nowNs,
		); err != nil {
			return 0, fmt.Errorf("sheep: upsert scoped_memories: %w", err)
		}

		if _, err := tx.ExecContext(ctx, `
			DELETE FROM memory_symbols WHERE scope_type = ? AND scope_id = ? AND memory_key = ?`,
			scopeType, scopeID, g.normKey,
		); err != nil {
			return 0, err
		}
		for _, sym := range normSyms {
			if _, err := tx.ExecContext(ctx, `
				INSERT OR IGNORE INTO memory_symbols (scope_type, scope_id, memory_key, repo_root, symbol)
				VALUES (?, ?, ?, ?, ?)`,
				scopeType, scopeID, g.normKey, g.repoRoot, sym,
			); err != nil {
				return 0, err
			}
		}

		docKey := fmt.Sprintf("mem:%s|%s|%s", scopeType, scopeID, g.normKey)
		if err := indexTokensTx(ctx, tx, s.ftsEnabled, "mem", docKey, g.repoRoot, g.scope, repContent+" "+strings.Join(normSyms, " "), tokens); err != nil {
			return 0, err
		}
		promoted++
	}

	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("sheep: commit dream consolidation: %w", err)
	}
	return promoted, nil
}

// EvictRenamedSymbols rewrites symbol bindings when renamed (new != "") or evicts stale symbol notes when deleted (new == "").
func (s *Store) EvictRenamedSymbols(ctx context.Context, repoRoot string, oldToNew map[string]string) error {
	if len(oldToNew) == 0 {
		return nil
	}
	normRepo := normalizeRepoRoot(repoRoot)

	// Deterministic execution order: process renames first, then deletions.
	var renames, deletions []string
	for oldSym, newSym := range oldToNew {
		oldTrim := strings.TrimSpace(oldSym)
		newTrim := strings.TrimSpace(newSym)
		if oldTrim == "" || oldTrim == newTrim {
			continue
		}
		if newTrim != "" {
			renames = append(renames, oldSym)
		} else {
			deletions = append(deletions, oldSym)
		}
	}
	sort.Strings(renames)
	sort.Strings(deletions)
	orderedKeys := append(renames, deletions...)
	if len(orderedKeys) == 0 {
		return nil
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("sheep: begin evict tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	for _, rawOld := range orderedKeys {
		oldSym := strings.TrimSpace(rawOld)
		newSym := strings.TrimSpace(oldToNew[rawOld])

		if err := s.applySymbolChangeObservationsTx(ctx, tx, normRepo, oldSym, newSym); err != nil {
			return err
		}
		if err := s.applySymbolChangeMemoriesTx(ctx, tx, normRepo, oldSym, newSym); err != nil {
			return err
		}
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("sheep: commit evict: %w", err)
	}
	return nil
}

func rewriteSymbolList(syms []string, oldSym, newSym string) []string {
	updated := make([]string, 0, len(syms))
	for _, sym := range syms {
		if sym == oldSym {
			if newSym != "" {
				updated = append(updated, newSym)
			}
		} else {
			updated = append(updated, sym)
		}
	}
	return normalizeSymbols(updated)
}

func (s *Store) applySymbolChangeObservationsTx(ctx context.Context, tx *sql.Tx, normRepo, oldSym, newSym string) error {
	type obsRecord struct {
		id      int64
		scope   string
		content string
		syms    []string
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT o.observation_id, o.scope, o.content, o.symbols_json
		FROM observations o
		JOIN observation_symbols os ON o.observation_id = os.observation_id
		WHERE os.repo_root = ? AND os.symbol = ?`,
		normRepo, oldSym,
	)
	if err != nil {
		return fmt.Errorf("sheep: query symbol observations: %w", err)
	}
	var records []obsRecord
	for rows.Next() {
		var r obsRecord
		var symsJSON string
		if err := rows.Scan(&r.id, &r.scope, &r.content, &symsJSON); err != nil {
			_ = rows.Close()
			return err
		}
		_ = json.Unmarshal([]byte(symsJSON), &r.syms)
		records = append(records, r)
	}
	_ = rows.Close()

	for _, r := range records {
		docKey := fmt.Sprintf("obs:%d", r.id)
		nextSyms := rewriteSymbolList(r.syms, oldSym, newSym)
		if newSym == "" && len(nextSyms) == 0 {
			// Exclusively bound to deleted symbol -> evict stale observation completely.
			if _, err := tx.ExecContext(ctx, `DELETE FROM observations WHERE observation_id = ?`, r.id); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `DELETE FROM lexical_tokens WHERE doc_kind = 'obs' AND doc_key = ?`, docKey); err != nil {
				return err
			}
			if s.ftsEnabled {
				_, _ = tx.ExecContext(ctx, `DELETE FROM memories_fts WHERE doc_kind = 'obs' AND doc_key = ?`, docKey)
			}
			continue
		}

		nextContent := r.content
		if newSym != "" {
			nextContent = strings.ReplaceAll(nextContent, oldSym, newSym)
		}
		symsBytes, _ := json.Marshal(nextSyms)
		if nextSyms == nil {
			symsBytes = []byte("[]")
		}
		tokens := extractLexicalTokens(nextContent, nextSyms)
		keywords := strings.Join(tokens, " ")
		normKey := canonicalObservationKey(nextContent)

		if _, err := tx.ExecContext(ctx, `
			UPDATE observations
			SET content = ?, normalized_key = ?, keywords = ?, symbols_json = ?
			WHERE observation_id = ?`,
			nextContent, normKey, keywords, string(symsBytes), r.id,
		); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM observation_symbols WHERE observation_id = ?`, r.id); err != nil {
			return err
		}
		for _, sym := range nextSyms {
			if _, err := tx.ExecContext(ctx, `
				INSERT OR IGNORE INTO observation_symbols (observation_id, repo_root, symbol)
				VALUES (?, ?, ?)`,
				r.id, normRepo, sym,
			); err != nil {
				return err
			}
		}
		if err := indexTokensTx(ctx, tx, s.ftsEnabled, "obs", docKey, normRepo, r.scope, nextContent+" "+strings.Join(nextSyms, " "), tokens); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) applySymbolChangeMemoriesTx(ctx context.Context, tx *sql.Tx, normRepo, oldSym, newSym string) error {
	type memRecord struct {
		scopeType string
		scopeID   string
		memKey    string
		belief    string
		syms      []string
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT sm.scope_type, sm.scope_id, sm.memory_key, sm.crystallized_belief, sm.symbols_json
		FROM scoped_memories sm
		JOIN memory_symbols ms
		  ON sm.scope_type = ms.scope_type
		 AND sm.scope_id = ms.scope_id
		 AND sm.memory_key = ms.memory_key
		WHERE ms.repo_root = ? AND ms.symbol = ?`,
		normRepo, oldSym,
	)
	if err != nil {
		return fmt.Errorf("sheep: query symbol memories: %w", err)
	}
	var records []memRecord
	for rows.Next() {
		var r memRecord
		var symsJSON string
		if err := rows.Scan(&r.scopeType, &r.scopeID, &r.memKey, &r.belief, &symsJSON); err != nil {
			_ = rows.Close()
			return err
		}
		_ = json.Unmarshal([]byte(symsJSON), &r.syms)
		records = append(records, r)
	}
	_ = rows.Close()

	nowNs := time.Now().UTC().UnixNano()
	for _, r := range records {
		docKey := fmt.Sprintf("mem:%s|%s|%s", r.scopeType, r.scopeID, r.memKey)
		nextSyms := rewriteSymbolList(r.syms, oldSym, newSym)
		if newSym == "" && len(nextSyms) == 0 {
			if _, err := tx.ExecContext(ctx, `
				DELETE FROM scoped_memories WHERE scope_type = ? AND scope_id = ? AND memory_key = ?`,
				r.scopeType, r.scopeID, r.memKey,
			); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `DELETE FROM lexical_tokens WHERE doc_kind = 'mem' AND doc_key = ?`, docKey); err != nil {
				return err
			}
			if s.ftsEnabled {
				_, _ = tx.ExecContext(ctx, `DELETE FROM memories_fts WHERE doc_kind = 'mem' AND doc_key = ?`, docKey)
			}
			continue
		}

		nextBelief := r.belief
		if newSym != "" {
			nextBelief = strings.ReplaceAll(nextBelief, oldSym, newSym)
		}
		symsBytes, _ := json.Marshal(nextSyms)
		if nextSyms == nil {
			symsBytes = []byte("[]")
		}
		tokens := extractLexicalTokens(nextBelief, nextSyms)
		keywords := strings.Join(tokens, " ")

		if _, err := tx.ExecContext(ctx, `
			UPDATE scoped_memories
			SET memory_value = ?, crystallized_belief = ?, keywords = ?, symbols_json = ?, last_updated_ns = ?
			WHERE scope_type = ? AND scope_id = ? AND memory_key = ?`,
			nextBelief, nextBelief, keywords, string(symsBytes), nowNs,
			r.scopeType, r.scopeID, r.memKey,
		); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			DELETE FROM memory_symbols WHERE scope_type = ? AND scope_id = ? AND memory_key = ?`,
			r.scopeType, r.scopeID, r.memKey,
		); err != nil {
			return err
		}
		for _, sym := range nextSyms {
			if _, err := tx.ExecContext(ctx, `
				INSERT OR IGNORE INTO memory_symbols (scope_type, scope_id, memory_key, repo_root, symbol)
				VALUES (?, ?, ?, ?, ?)`,
				r.scopeType, r.scopeID, r.memKey, normRepo, sym,
			); err != nil {
				return err
			}
		}
		if err := indexTokensTx(ctx, tx, s.ftsEnabled, "mem", docKey, normRepo, "/mem/"+r.scopeType, nextBelief+" "+strings.Join(nextSyms, " "), tokens); err != nil {
			return err
		}
	}
	return nil
}

type recallCandidate struct {
	prefix       string
	body         string
	tokens       []string
	confidence   float64
	supportCount int
	crystallized bool
	createdAtNs  int64
	score        float64
}

func computeLexicalOverlap(intentSet map[string]struct{}, docTokens []string) float64 {
	if len(intentSet) == 0 || len(docTokens) == 0 {
		return 0
	}
	matches := 0
	for _, tok := range docTokens {
		if _, ok := intentSet[tok]; ok {
			matches++
		}
	}
	if matches == 0 {
		return 0
	}
	return float64(matches) / math.Sqrt(float64(len(intentSet))*float64(len(docTokens)))
}

// RecallTurnZero assembles the Turn-0 agent memory frame obeying utp.ContextMount byte walls:
//   - utp.ContextClean: hard 0-history wall ("", nil)
//   - utp.ContextScoped: <= 2048 bytes ranked repo/intent crystallized beliefs + promoted observations
//   - utp.ContextFull: <= 3800 bytes full relevant repo & personal memory context
func (s *Store) RecallTurnZero(ctx context.Context, repoRoot, intent string, mount utp.ContextMount) (string, error) {
	if mount == "" {
		mount = utp.ContextScoped
	}
	switch mount {
	case utp.ContextClean:
		return "", nil
	case utp.ContextScoped, utp.ContextFull:
	default:
		return "", fmt.Errorf("%w: %q", ErrInvalidMount, mount)
	}

	maxBytes := MaxScopedRecallBytes
	if mount == utp.ContextFull {
		maxBytes = MaxFullRecallBytes
	}

	normRepo := normalizeRepoRoot(repoRoot)
	intentTokens := extractLexicalTokens(intent, nil)
	intentSet := make(map[string]struct{}, len(intentTokens))
	for _, tok := range intentTokens {
		intentSet[tok] = struct{}{}
	}

	var candidates []recallCandidate
	promotedKeys := make(map[string]struct{})

	// 1. Load crystallized beliefs across /mem/personal, /mem/project, and /mem/org.
	memRows, err := s.db.QueryContext(ctx, `
		SELECT scope_type, memory_key, crystallized_belief, keywords, confidence, support_count, last_updated_ns
		FROM scoped_memories
		WHERE (scope_type = 'project' AND scope_id = ?)
		   OR (scope_type = 'personal' AND scope_id IN (?, 'global'))
		   OR (scope_type = 'org' AND scope_id IN (?, 'global'))`,
		normRepo, normRepo, normRepo,
	)
	if err != nil {
		return "", fmt.Errorf("sheep: recall scoped_memories: %w", err)
	}
	for memRows.Next() {
		var scopeType, memKey, belief, keywords string
		var conf float64
		var support int
		var updatedNs int64
		if err := memRows.Scan(&scopeType, &memKey, &belief, &keywords, &conf, &support, &updatedNs); err != nil {
			_ = memRows.Close()
			return "", err
		}
		promotedKeys[scopeType+"|"+memKey] = struct{}{}
		candidates = append(candidates, recallCandidate{
			prefix:       "/mem/" + scopeType,
			body:         belief,
			tokens:       strings.Fields(keywords),
			confidence:   conf,
			supportCount: support,
			crystallized: true,
			createdAtNs:  updatedNs,
		})
	}
	_ = memRows.Close()

	// 2. Query external HTTP dark-data mounts when registered.
	darkNotes, err := s.queryDarkDataMounts(ctx, normRepo, intent)
	if err != nil {
		return "", err
	}
	candidates = append(candidates, darkNotes...)

	// 3. For ContextFull (or when zero crystallized beliefs exist yet), include distinct unpromoted observations.
	if mount == utp.ContextFull || len(candidates) == 0 {
		obsRows, err := s.db.QueryContext(ctx, `
			SELECT scope, normalized_key, content, keywords, COUNT(DISTINCT session_id) AS support, MAX(created_at_ns) AS latest_ns
			FROM observations
			WHERE repo_root = ? OR scope IN ('/mem/personal', '/mem/org')
			GROUP BY scope, normalized_key
			ORDER BY latest_ns DESC
			LIMIT 64`,
			normRepo,
		)
		if err != nil {
			return "", fmt.Errorf("sheep: recall observations: %w", err)
		}
		for obsRows.Next() {
			var scope, normKey, content, keywords string
			var support int
			var latestNs int64
			if err := obsRows.Scan(&scope, &normKey, &content, &keywords, &support, &latestNs); err != nil {
				_ = obsRows.Close()
				return "", err
			}
			sType := scopeToType(scope)
			if _, alreadyPromoted := promotedKeys[sType+"|"+normKey]; alreadyPromoted {
				continue
			}
			candidates = append(candidates, recallCandidate{
				prefix:       scope,
				body:         content,
				tokens:       strings.Fields(keywords),
				confidence:   0.50,
				supportCount: support,
				crystallized: false,
				createdAtNs:  latestNs,
			})
		}
		_ = obsRows.Close()
	} else if mount == utp.ContextScoped && len(intentSet) > 0 {
		// Also surface matching pre-edit page-fault navigation bindings ([nav] ...)
		// on Turn-0 scoped recall so the very next session resolves missed symbols with zero cold grep.
		navRows, err := s.db.QueryContext(ctx, `
			SELECT scope, normalized_key, content, keywords, COUNT(DISTINCT session_id) AS support, MAX(created_at_ns) AS latest_ns
			FROM observations
			WHERE repo_root = ? AND content LIKE '[nav] %'
			GROUP BY scope, normalized_key
			ORDER BY latest_ns DESC
			LIMIT 32`,
			normRepo,
		)
		if err != nil {
			return "", fmt.Errorf("sheep: recall nav observations: %w", err)
		}
		for navRows.Next() {
			var scope, normKey, content, keywords string
			var support int
			var latestNs int64
			if err := navRows.Scan(&scope, &normKey, &content, &keywords, &support, &latestNs); err != nil {
				_ = navRows.Close()
				return "", err
			}
			sType := scopeToType(scope)
			if _, alreadyPromoted := promotedKeys[sType+"|"+normKey]; alreadyPromoted {
				continue
			}
			toks := strings.Fields(keywords)
			if computeLexicalOverlap(intentSet, toks) <= 0 {
				continue
			}
			candidates = append(candidates, recallCandidate{
				prefix:       scope,
				body:         content,
				tokens:       toks,
				confidence:   0.50,
				supportCount: support,
				crystallized: false,
				createdAtNs:  latestNs,
			})
		}
		_ = navRows.Close()
	}

	if len(candidates) == 0 {
		return "", nil
	}

	for i := range candidates {
		c := &candidates[i]
		overlap := computeLexicalOverlap(intentSet, c.tokens)
		scopeBoost := 0.25
		switch c.prefix {
		case "/mem/org":
			scopeBoost = 0.40
		case "/mem/project":
			scopeBoost = 0.35
		case "/mem/personal":
			scopeBoost = 0.25
		}
		crystBoost := 0.0
		if c.crystallized {
			crystBoost = 1.25
		} else if strings.HasPrefix(c.body, loops.NavObservationPrefix) && overlap > 0 {
			crystBoost = 1.15
		}
		supportBoost := 0.08 * math.Min(float64(c.supportCount), 10)
		c.score = 4.0*overlap + crystBoost + scopeBoost + c.confidence + supportBoost
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		if math.Abs(candidates[i].score-candidates[j].score) > 1e-9 {
			return candidates[i].score > candidates[j].score
		}
		return candidates[i].createdAtNs > candidates[j].createdAtNs
	})

	header := fmt.Sprintf("# Sheep Turn-0 Memory (%s)\n", mount)
	var b strings.Builder
	b.WriteString(header)

	added := 0
	for _, c := range candidates {
		tag := c.prefix
		if !c.crystallized {
			tag += ":obs"
		}
		line := fmt.Sprintf("- [%s] %s\n", tag, c.body)
		if b.Len()+len(line) > maxBytes {
			if added == 0 {
				avail := maxBytes - b.Len() - 1
				if avail > 16 {
					b.WriteString(truncateUTF8Bytes(strings.TrimSuffix(line, "\n"), avail))
					b.WriteByte('\n')
					added++
				}
			}
			break
		}
		b.WriteString(line)
		added++
	}

	if added == 0 {
		return "", nil
	}
	return truncateUTF8Bytes(b.String(), maxBytes), nil
}

// HasDeliveredNote reports whether noteKey was already injected for sessionID.
func (s *Store) HasDeliveredNote(ctx context.Context, sessionID, noteKey string) (bool, error) {
	sessionID = strings.TrimSpace(sessionID)
	noteKey = strings.TrimSpace(noteKey)
	if sessionID == "" || noteKey == "" {
		return false, nil
	}
	var exists int
	err := s.db.QueryRowContext(ctx, `
		SELECT 1 FROM session_delivered_notes
		WHERE session_id = ? AND note_key = ?
		LIMIT 1`,
		sessionID, noteKey,
	).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("sheep: check delivered note: %w", err)
	}
	return true, nil
}

// MarkDeliveredNote records that noteKey was delivered to sessionID so subsequent
// PostToolUse calls in the same session do not repeat the same note.
func (s *Store) MarkDeliveredNote(ctx context.Context, sessionID, noteKey string) error {
	sessionID = strings.TrimSpace(sessionID)
	noteKey = strings.TrimSpace(noteKey)
	if sessionID == "" || noteKey == "" {
		return nil
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT OR IGNORE INTO session_delivered_notes (session_id, note_key, delivered_at_ns)
		VALUES (?, ?, ?)`,
		sessionID, noteKey, time.Now().UTC().UnixNano(),
	)
	if err != nil {
		return fmt.Errorf("sheep: mark delivered note: %w", err)
	}
	return nil
}

// ResetDeliveredNotes clears delivered PostToolUse note state for sessionID.
func (s *Store) ResetDeliveredNotes(ctx context.Context, sessionID string) error {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return nil
	}
	_, err := s.db.ExecContext(ctx, `DELETE FROM session_delivered_notes WHERE session_id = ?`, sessionID)
	if err != nil {
		return fmt.Errorf("sheep: reset delivered notes: %w", err)
	}
	return nil
}

// FaultTracker returns the Store's shared pre-edit PageFaultTracker instance.
func (s *Store) FaultTracker() *loops.PageFaultTracker {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.faultTracker == nil {
		s.faultTracker = loops.NewPageFaultTracker(s)
	}
	return s.faultTracker
}

// RecallInjector returns the Store's shared PostToolUse SymbolRecallInjector instance.
func (s *Store) RecallInjector() *loops.SymbolRecallInjector {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.recallInjector == nil {
		s.recallInjector = loops.NewSymbolRecallInjector(s)
	}
	return s.recallInjector
}
