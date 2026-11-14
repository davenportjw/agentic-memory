package sheep_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"unicode/utf8"

	"github.com/davenportjw/electric-sheep/pkg/sheep"
	"github.com/davenportjw/toolroll/utp"
)

// kernelMemoryStore mirrors toolroll/pkg/kernel.MemoryStore byte-for-byte
// without importing root github.com/davenportjw/toolroll.
type kernelMemoryStore interface {
	RecallTurnZero(ctx context.Context, repoRoot, intent string, mount utp.ContextMount) (frame string, err error)
	SymbolNote(ctx context.Context, repoRoot, symbol string) (note string, err error)
	Observe(ctx context.Context, repoRoot, sessionID, scope, content string, symbols []string) error
	EvictRenamedSymbols(ctx context.Context, repoRoot string, oldToNew map[string]string) error
	DreamConsolidate(ctx context.Context, repoRoot string) (promoted int, err error)
}

var _ kernelMemoryStore = (*sheep.Store)(nil)

func openTestStore(t *testing.T) (*sheep.Store, string) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "memory.db")
	st, err := sheep.Open(dbPath)
	if err != nil {
		t.Fatalf("sheep.Open(%s): %v", dbPath, err)
	}
	t.Cleanup(func() { _ = st.Close() })
	repo := filepath.Join(dir, "repo-alpha")
	return st, repo
}

func randomToken(t *testing.T) string {
	t.Helper()
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		t.Fatalf("rand.Read: %v", err)
	}
	return hex.EncodeToString(b)
}

func TestScopeNormalizationAndObserveValidation(t *testing.T) {
	st, repo := openTestStore(t)
	ctx := context.Background()

	cases := map[string]string{
		"":              "/mem/project",
		"project":       "/mem/project",
		"/mem/project":  "/mem/project",
		"personal":      "/mem/personal",
		"/mem/personal": "/mem/personal",
		"org":           "/mem/org",
		"/mem/org":      "/mem/org",
	}
	for in, want := range cases {
		got, err := sheep.NormalizeScope(in)
		if err != nil {
			t.Fatalf("NormalizeScope(%q) unexpected err: %v", in, err)
		}
		if got != want {
			t.Fatalf("NormalizeScope(%q) = %q, want %q", in, got, want)
		}
	}

	if _, err := sheep.NormalizeScope("/mem/unknown"); err == nil {
		t.Fatalf("expected error for invalid scope /mem/unknown")
	}

	if err := st.Observe(ctx, repo, "", "/mem/project", "missing session", nil); err == nil {
		t.Fatalf("expected error for empty sessionID")
	}
	if err := st.Observe(ctx, repo, "sess-1", "/mem/project", "   ", nil); err == nil {
		t.Fatalf("expected error for blank content")
	}
	if err := st.Observe(ctx, repo, "sess-1", "/mem/bogus", "valid text", nil); err == nil {
		t.Fatalf("expected error for invalid scope")
	}
}

func TestObserveAndSymbolNoteByteCap(t *testing.T) {
	st, repo := openTestStore(t)
	ctx := context.Background()

	// Unknown symbol returns "", nil.
	note, err := st.SymbolNote(ctx, repo, "UnknownSymbol")
	if err != nil {
		t.Fatalf("SymbolNote unknown err: %v", err)
	}
	if note != "" {
		t.Fatalf("SymbolNote unknown = %q, want empty", note)
	}

	// Long symbol hazard note (> 450 bytes including multi-byte UTF-8 runes) must be capped <= 200 bytes cleanly.
	longHazard := "WebhookDispatcher retry loop hazard — must back off exponentially on HTTP 429/503 with jitter and honor Retry-After header; never hold g.mu lock across network round-trips or channel sends (Δt <= 5s, see ADR-004 §2.1 — verificatión UTF-8 multibyte safety check αβγδεζηθικλμνξοπρστυφχψω)."
	if err := st.Observe(ctx, repo, "sess-1", "project", longHazard, []string{"WebhookDispatcher", "RetryPolicy"}); err != nil {
		t.Fatalf("Observe failed: %v", err)
	}

	note, err = st.SymbolNote(ctx, repo, "WebhookDispatcher")
	if err != nil {
		t.Fatalf("SymbolNote WebhookDispatcher err: %v", err)
	}
	if note == "" {
		t.Fatalf("expected non-empty SymbolNote for WebhookDispatcher")
	}
	if len(note) > 200 {
		t.Fatalf("SymbolNote exceeded 200-byte cap: got %d bytes (%q)", len(note), note)
	}
	if !utf8.ValidString(note) {
		t.Fatalf("SymbolNote truncated invalid UTF-8: %q", note)
	}
	if !strings.Contains(note, "WebhookDispatcher") && !strings.Contains(note, "429") {
		t.Fatalf("SymbolNote missing hazard substance: %q", note)
	}
}

func TestDreamConsolidateDistinctSessionThreshold(t *testing.T) {
	st, repo := openTestStore(t)
	ctx := context.Background()

	obs := "Always pass GOWORK=off CGO_ENABLED=0 when running Go tests in isolated worktrees"

	// Same session repeated twice + second session once = 2 distinct sessions (< 3).
	if err := st.Observe(ctx, repo, "sess-a", "/mem/project", obs, []string{"RunTests"}); err != nil {
		t.Fatalf("Observe sess-a (1): %v", err)
	}
	if err := st.Observe(ctx, repo, "sess-a", "/mem/project", obs+".", []string{"RunTests"}); err != nil {
		t.Fatalf("Observe sess-a (2): %v", err)
	}
	if err := st.Observe(ctx, repo, "sess-b", "/mem/project", strings.ToLower(obs), []string{"RunTests"}); err != nil {
		t.Fatalf("Observe sess-b: %v", err)
	}

	promoted, err := st.DreamConsolidate(ctx, repo)
	if err != nil {
		t.Fatalf("DreamConsolidate (2 sessions): %v", err)
	}
	if promoted != 0 {
		t.Fatalf("expected 0 promotions with 2 distinct sessions, got %d", promoted)
	}

	// Third distinct session triggers promotion into crystallized belief!
	if err := st.Observe(ctx, repo, "sess-c", "/mem/project", obs+"!", []string{"TestRunner"}); err != nil {
		t.Fatalf("Observe sess-c: %v", err)
	}

	promoted, err = st.DreamConsolidate(ctx, repo)
	if err != nil {
		t.Fatalf("DreamConsolidate (3 sessions): %v", err)
	}
	if promoted != 1 {
		t.Fatalf("expected 1 promoted belief after 3 distinct sessions, got %d", promoted)
	}

	// Idempotent re-run with no new distinct sessions returns 0.
	promotedAgain, err := st.DreamConsolidate(ctx, repo)
	if err != nil {
		t.Fatalf("DreamConsolidate idempotent re-run: %v", err)
	}
	if promotedAgain != 0 {
		t.Fatalf("expected 0 promotions on idempotent re-run, got %d", promotedAgain)
	}

	// Fourth distinct session upgrades support count and confidence.
	if err := st.Observe(ctx, repo, "sess-d", "/mem/project", obs, []string{"RunTests"}); err != nil {
		t.Fatalf("Observe sess-d: %v", err)
	}
	upgraded, err := st.DreamConsolidate(ctx, repo)
	if err != nil {
		t.Fatalf("DreamConsolidate (4 sessions): %v", err)
	}
	if upgraded != 1 {
		t.Fatalf("expected 1 upgraded belief on 4th session, got %d", upgraded)
	}

	// Merged symbol bindings across sessions (RunTests + TestRunner) both resolve via SymbolNote.
	for _, sym := range []string{"RunTests", "TestRunner"} {
		n, err := st.SymbolNote(ctx, repo, sym)
		if err != nil {
			t.Fatalf("SymbolNote(%s): %v", sym, err)
		}
		if n == "" || len(n) > 200 {
			t.Fatalf("SymbolNote(%s) invalid: len=%d note=%q", sym, len(n), n)
		}
	}
}

func TestRecallTurnZeroContextMountsAndCaps(t *testing.T) {
	st, repo := openTestStore(t)
	ctx := context.Background()

	// Seed promoted beliefs across /mem/personal, /mem/project, /mem/org + dense extra memories (> 5 KB total).
	for _, sess := range []string{"s1", "s2", "s3"} {
		if err := st.Observe(ctx, repo, sess, "/mem/personal", "Prefer uv run pytest for Python test suites on Apple Silicon", nil); err != nil {
			t.Fatalf("personal observe: %v", err)
		}
		if err := st.Observe(ctx, repo, sess, "/mem/project", "WebhookDispatcher exponential backoff uses base 200ms and Retry-After header", []string{"WebhookDispatcher"}); err != nil {
			t.Fatalf("project observe: %v", err)
		}
		if err := st.Observe(ctx, repo, sess, "/mem/org", "Never commit static API keys or seed tokens; enforce ADC auth across services", nil); err != nil {
			t.Fatalf("org observe: %v", err)
		}
	}

	// Add 25 promoted background beliefs and 15 unpromoted session observations to exceed byte caps.
	for i := 0; i < 25; i++ {
		msg := fmt.Sprintf("Subsystem module-%02d invariant: preserve idempotent WAL framing with CRC32 verification across batch commits and compaction checkpoints", i)
		for _, sess := range []string{"s1", "s2", "s3"} {
			if err := st.Observe(ctx, repo, sess, "/mem/project", msg, []string{fmt.Sprintf("Module%02d", i)}); err != nil {
				t.Fatalf("bulk promoted observe %d: %v", i, err)
			}
		}
	}
	for i := 0; i < 15; i++ {
		raw := fmt.Sprintf("Unpromoted raw session note %02d on WebhookDispatcher queue backpressure telemetry counters", i)
		if err := st.Observe(ctx, repo, fmt.Sprintf("solo-%d", i), "/mem/project", raw, []string{"WebhookDispatcher"}); err != nil {
			t.Fatalf("raw observe %d: %v", i, err)
		}
	}

	if _, err := st.DreamConsolidate(ctx, repo); err != nil {
		t.Fatalf("DreamConsolidate: %v", err)
	}

	// 1. ContextClean: hard 0-history wall -> "", nil!
	cleanFrame, err := st.RecallTurnZero(ctx, repo, "WebhookDispatcher backoff", utp.ContextClean)
	if err != nil {
		t.Fatalf("RecallTurnZero clean err: %v", err)
	}
	if cleanFrame != "" {
		t.Fatalf("ContextClean must return empty string, got %d bytes: %q", len(cleanFrame), cleanFrame)
	}

	// 2. ContextScoped: <= 2048 bytes (<2 KB) and prioritizes intent match ("WebhookDispatcher backoff").
	scopedFrame, err := st.RecallTurnZero(ctx, repo, "WebhookDispatcher exponential backoff", utp.ContextScoped)
	if err != nil {
		t.Fatalf("RecallTurnZero scoped err: %v", err)
	}
	if scopedFrame == "" {
		t.Fatalf("expected non-empty scoped frame")
	}
	if len(scopedFrame) > 2048 {
		t.Fatalf("ContextScoped exceeded 2048 bytes: got %d bytes", len(scopedFrame))
	}
	if !utf8.ValidString(scopedFrame) {
		t.Fatalf("ContextScoped invalid UTF-8")
	}
	if !strings.Contains(scopedFrame, "WebhookDispatcher") {
		t.Fatalf("ContextScoped failed to rank WebhookDispatcher belief into frame:\n%s", scopedFrame)
	}

	// 3. ContextFull: <= 3800 bytes and richer than scoped.
	fullFrame, err := st.RecallTurnZero(ctx, repo, "WebhookDispatcher queue backpressure", utp.ContextFull)
	if err != nil {
		t.Fatalf("RecallTurnZero full err: %v", err)
	}
	if len(fullFrame) > 3800 {
		t.Fatalf("ContextFull exceeded 3800 bytes: got %d bytes", len(fullFrame))
	}
	if len(fullFrame) <= len(scopedFrame) {
		t.Fatalf("expected ContextFull (%d B) > ContextScoped (%d B)", len(fullFrame), len(scopedFrame))
	}
	if !utf8.ValidString(fullFrame) {
		t.Fatalf("ContextFull invalid UTF-8")
	}
}

func TestEvictRenamedAndDeletedSymbols(t *testing.T) {
	st, repo := openTestStore(t)
	ctx := context.Background()

	for _, sess := range []string{"s1", "s2", "s3"} {
		if err := st.Observe(ctx, repo, sess, "/mem/project", "LegacyDispatcher must drain inflight webhooks before shutdown", []string{"LegacyDispatcher"}); err != nil {
			t.Fatalf("Observe LegacyDispatcher: %v", err)
		}
		if err := st.Observe(ctx, repo, sess, "/mem/project", "DeprecatedPoller polls legacy v0 endpoint every 30s", []string{"DeprecatedPoller"}); err != nil {
			t.Fatalf("Observe DeprecatedPoller: %v", err)
		}
	}
	if _, err := st.DreamConsolidate(ctx, repo); err != nil {
		t.Fatalf("DreamConsolidate: %v", err)
	}

	if err := st.EvictRenamedSymbols(ctx, repo, map[string]string{
		"LegacyDispatcher": "ModernDispatcher",
		"DeprecatedPoller": "",
	}); err != nil {
		t.Fatalf("EvictRenamedSymbols: %v", err)
	}

	// Old renamed symbol is empty; new symbol carries the migrated note.
	if oldNote, err := st.SymbolNote(ctx, repo, "LegacyDispatcher"); err != nil || oldNote != "" {
		t.Fatalf("expected empty note for LegacyDispatcher, got %q (err=%v)", oldNote, err)
	}
	newNote, err := st.SymbolNote(ctx, repo, "ModernDispatcher")
	if err != nil || newNote == "" {
		t.Fatalf("expected migrated note for ModernDispatcher, got %q (err=%v)", newNote, err)
	}

	// Deleted symbol is completely evicted from both SymbolNote and RecallTurnZero.
	if delNote, err := st.SymbolNote(ctx, repo, "DeprecatedPoller"); err != nil || delNote != "" {
		t.Fatalf("expected empty note for deleted DeprecatedPoller, got %q (err=%v)", delNote, err)
	}
	frame, err := st.RecallTurnZero(ctx, repo, "DeprecatedPoller legacy endpoint", utp.ContextFull)
	if err != nil {
		t.Fatalf("RecallTurnZero after eviction: %v", err)
	}
	if strings.Contains(frame, "DeprecatedPoller") {
		t.Fatalf("RecallTurnZero leaked evicted symbol DeprecatedPoller:\n%s", frame)
	}
}

func TestMountsSpannerFederatedSyncAndDarkDataHTTP(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	repo := filepath.Join(dir, "repo-federated")
	bearerToken := randomToken(t)

	// In-process Spanner v1 REST SQL/Mutation protocol endpoint verifying sql/spanner_schema.sql ScopedMemories columns.
	var mu sync.Mutex
	remoteRows := map[string]sheep.ScopedMemoryRow{
		"org|global|org_zero_secrets": {
			ScopeType:          "org",
			ScopeId:            "global",
			MemoryKey:          "org_zero_secrets",
			MemoryValue:        "Enforce Secret Manager and ADC across all production services",
			MemoryType:         "policy",
			Category:           "security",
			Confidence:         0.99,
			CrystallizedBelief: "Enforce Secret Manager and ADC across all production services",
			Keywords:           "secret manager adc security",
			SupportCount:       10,
		},
	}

	spannerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+bearerToken {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/sessions") && r.Method == http.MethodPost:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"name": "projects/davenport-boutique/instances/sheep-inst/databases/sheep-db/sessions/s1",
			})
		case strings.HasSuffix(r.URL.Path, ":commit") && r.Method == http.MethodPost:
			var payload struct {
				Mutations []struct {
					InsertOrUpdate struct {
						Table   string   `json:"table"`
						Columns []string `json:"columns"`
						Values  [][]any  `json:"values"`
					} `json:"insertOrUpdate"`
				} `json:"mutations"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			mu.Lock()
			for _, m := range payload.Mutations {
				iu := m.InsertOrUpdate
				if iu.Table != "ScopedMemories" {
					mu.Unlock()
					http.Error(w, "unexpected table "+iu.Table, http.StatusBadRequest)
					return
				}
				colIdx := map[string]int{}
				for idx, c := range iu.Columns {
					colIdx[c] = idx
				}
				for _, vals := range iu.Values {
					strAt := func(col string) string {
						if i, ok := colIdx[col]; ok && i < len(vals) && vals[i] != nil {
							return fmt.Sprint(vals[i])
						}
						return ""
					}
					row := sheep.ScopedMemoryRow{
						ScopeType:          strAt("ScopeType"),
						ScopeId:            strAt("ScopeId"),
						MemoryKey:          strAt("MemoryKey"),
						MemoryValue:        strAt("MemoryValue"),
						MemoryType:         strAt("MemoryType"),
						Category:           strAt("Category"),
						CrystallizedBelief: strAt("CrystallizedBelief"),
						Keywords:           strAt("Keywords"),
						SourceSessionId:    strAt("SourceSessionId"),
						PromotedFromScope:  strAt("PromotedFromScope"),
						Confidence:         0.85,
						SupportCount:       3,
					}
					remoteRows[row.ScopeType+"|"+row.ScopeId+"|"+row.MemoryKey] = row
				}
			}
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{"commitTimestamp": "2026-11-14T00:00:00Z"})
		case strings.HasSuffix(r.URL.Path, ":executeSql") && r.Method == http.MethodPost:
			mu.Lock()
			rows := make([][]any, 0, len(remoteRows))
			for _, rr := range remoteRows {
				rows = append(rows, []any{
					rr.ScopeType,
					rr.ScopeId,
					rr.MemoryKey,
					rr.MemoryValue,
					rr.MemoryType,
					rr.Category,
					rr.Confidence,
					rr.CrystallizedBelief,
					rr.Keywords,
					rr.SourceSessionId,
					rr.PromotedFromScope,
					fmt.Sprintf("%d", rr.SupportCount),
				})
			}
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{"rows": rows})
		default:
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{}`))
		}
	}))
	defer spannerSrv.Close()

	var darkCalls atomic.Int32
	darkSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		darkCalls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"notes": []map[string]any{
				{
					"key":        "inc-204",
					"content":    "Runbook INC-204: drain Redis consumer leases before rolling Cloud Run traffic",
					"confidence": 0.92,
				},
			},
		})
	}))
	defer darkSrv.Close()

	st, err := sheep.OpenWithConfig(sheep.Config{
		DBPath: filepath.Join(dir, "memory.db"),
		Spanner: &sheep.SpannerConfig{
			ProjectID:  "davenport-boutique",
			InstanceID: "sheep-inst",
			DatabaseID: "sheep-db",
			BaseURL:    spannerSrv.URL,
			TokenSource: func(ctx context.Context) (string, error) {
				return bearerToken, nil
			},
		},
	})
	if err != nil {
		t.Fatalf("OpenWithConfig: %v", err)
	}
	defer st.Close()

	if err := st.RegisterDarkDataMount(sheep.DarkDataMountConfig{
		Prefix:   "/mem/ext/incidents",
		Endpoint: darkSrv.URL,
	}); err != nil {
		t.Fatalf("RegisterDarkDataMount: %v", err)
	}

	mounts := st.ListMounts()
	if len(mounts) != 4 {
		t.Fatalf("expected 4 mounts (/mem/personal, /mem/project, /mem/org, /mem/ext/incidents), got %d: %+v", len(mounts), mounts)
	}
	if mounts[0].Prefix != "/mem/personal" || mounts[1].Prefix != "/mem/project" || !mounts[1].SyncEnabled || mounts[2].Prefix != "/mem/org" || !mounts[2].ReadOnly || mounts[3].Prefix != "/mem/ext/incidents" {
		t.Fatalf("unexpected mount descriptors: %+v", mounts)
	}

	// Consolidate a /mem/project belief locally and sync to Spanner + pull remote org belief.
	for _, sess := range []string{"s1", "s2", "s3"} {
		if err := st.Observe(ctx, repo, sess, "/mem/project", "Use atomic WAL checkpoints when flushing trajectory batches", []string{"FlushBatch"}); err != nil {
			t.Fatalf("Observe: %v", err)
		}
	}
	if _, err := st.DreamConsolidate(ctx, repo); err != nil {
		t.Fatalf("DreamConsolidate: %v", err)
	}

	pushed, pulled, err := st.SyncSpannerProject(ctx, repo)
	if err != nil {
		t.Fatalf("SyncSpannerProject: %v", err)
	}
	if pushed < 1 || pulled < 1 {
		t.Fatalf("expected pushed>=1 and pulled>=1, got pushed=%d pulled=%d", pushed, pulled)
	}

	// ContextClean must NOT call external dark-data HTTP provider.
	beforeCalls := darkCalls.Load()
	if clean, err := st.RecallTurnZero(ctx, repo, "Redis Cloud Run traffic", utp.ContextClean); err != nil || clean != "" {
		t.Fatalf("ContextClean unexpected result: %q err=%v", clean, err)
	}
	if darkCalls.Load() != beforeCalls {
		t.Fatalf("ContextClean leaked HTTP call to dark-data provider")
	}

	// ContextScoped includes both pulled Spanner org policy and HTTP dark-data incident note within <= 2048 B.
	scoped, err := st.RecallTurnZero(ctx, repo, "Secret Manager Redis Cloud Run traffic", utp.ContextScoped)
	if err != nil {
		t.Fatalf("RecallTurnZero scoped: %v", err)
	}
	if len(scoped) > 2048 {
		t.Fatalf("ContextScoped exceeded 2048B: %d", len(scoped))
	}
	if !strings.Contains(scoped, "Secret Manager") || !strings.Contains(scoped, "INC-204") {
		t.Fatalf("expected Spanner org policy and HTTP dark-data note in scoped recall:\n%s", scoped)
	}
}

func TestErrorSurfacingNoSilentFallback(t *testing.T) {
	st, repo := openTestStore(t)
	ctx := context.Background()

	// Unconfigured Spanner sync surfaces error directly without silent fallback.
	if _, _, err := st.SyncSpannerProject(ctx, repo); err == nil {
		t.Fatalf("expected error when SyncSpannerProject called without Spanner config")
	}

	// Failing HTTP dark-data mount surfaces HTTP error directly on non-clean recall.
	failSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "upstream dark-data unavailable", http.StatusBadGateway)
	}))
	defer failSrv.Close()

	if err := st.RegisterDarkDataMount(sheep.DarkDataMountConfig{
		Prefix:   "/mem/ext/failing",
		Endpoint: failSrv.URL,
	}); err != nil {
		t.Fatalf("RegisterDarkDataMount: %v", err)
	}
	if _, err := st.RecallTurnZero(ctx, repo, "test query", utp.ContextScoped); err == nil {
		t.Fatalf("expected upstream HTTP 502 error from failing dark-data mount, got nil")
	}
}
