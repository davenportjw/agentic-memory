package loops_test

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/davenportjw/electric-sheep/pkg/sheep"
	"github.com/davenportjw/electric-sheep/pkg/sheep/loops"
	"github.com/davenportjw/toolroll/utp"
)

func openLoopsTestStore(t *testing.T) (*sheep.Store, string) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "memory.db")
	st, err := sheep.Open(dbPath)
	if err != nil {
		t.Fatalf("sheep.Open(%q): %v", dbPath, err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st, filepath.Join(dir, "sample-repo")
}

func TestPageFaultClosedLoopZeroColdGrep(t *testing.T) {
	st, repo := openLoopsTestStore(t)
	ctx := context.Background()

	// Seed an existing unrelated crystallized belief in /mem/project so we verify
	// Turn-0 scoped recall surfaces page-fault navigation bindings even when
	// scoped_memories is already populated with unrelated beliefs.
	for _, sess := range []string{"seed-1", "seed-2", "seed-3"} {
		if err := st.Observe(ctx, repo, sess, "/mem/project",
			"DatabasePool max open connections must stay <= 4 under SQLite WAL mode",
			[]string{"DatabasePool"},
		); err != nil {
			t.Fatalf("seed observe: %v", err)
		}
	}
	if _, err := st.DreamConsolidate(ctx, repo); err != nil {
		t.Fatalf("DreamConsolidate: %v", err)
	}

	// Session 1: Cold Turn-0 recall for webhook retry jitter misses target symbols.
	session1ID := "sess-cold-01"
	prompt1 := "stabilize webhook retry jitter on HTTP 429 responses"
	coldFrame, err := st.RecallTurnZero(ctx, repo, prompt1, utp.ContextScoped)
	if err != nil {
		t.Fatalf("Session 1 RecallTurnZero: %v", err)
	}
	if strings.Contains(coldFrame, "WebhookJitterPolicy") || strings.Contains(coldFrame, "BackoffCalculator") {
		t.Fatalf("expected cold miss before page fault, got:\n%s", coldFrame)
	}

	// Session 1 suffers a 4-step read/grep page fault before its first mutation.
	tracker := loops.NewPageFaultTracker(st)
	readSpans := []utp.ToolSpan{
		{
			SpanID:     "sp-1",
			SessionID:  session1ID,
			ToolName:   "grep_search",
			TargetFile: "pkg/webhook",
			CreatedAt:  time.Now().UTC(),
		},
		{
			SpanID:     "sp-2",
			SessionID:  session1ID,
			ToolName:   "ast_outline",
			TargetFile: "pkg/webhook/dispatcher.go",
			CreatedAt:  time.Now().UTC(),
		},
		{
			SpanID:        "sp-3",
			SessionID:     session1ID,
			ToolName:      "read_file",
			TargetFile:    "pkg/webhook/jitter.go",
			TargetSymbols: []string{"BackoffCalculator"},
			CreatedAt:     time.Now().UTC(),
		},
		{
			SpanID:        "sp-4",
			SessionID:     session1ID,
			ToolName:      "ast_symbol",
			TargetFile:    "pkg/webhook/jitter.go",
			TargetSymbols: []string{"WebhookJitterPolicy"},
			CreatedAt:     time.Now().UTC(),
		},
	}

	for i, sp := range readSpans {
		pf, err := tracker.RecordSpan(ctx, repo, prompt1, sp)
		if err != nil {
			t.Fatalf("RecordSpan read step %d: %v", i+1, err)
		}
		if pf != nil {
			t.Fatalf("expected nil PageFault before first mutation on step %d, got %+v", i+1, pf)
		}
	}

	// First IsMutation == true span triggers PageFault emission + ProcessPageFault auto-binding!
	mutSpan := utp.ToolSpan{
		SpanID:        "sp-5",
		SessionID:     session1ID,
		ToolName:      "edit_file",
		TargetFile:    "pkg/webhook/jitter.go",
		TargetSymbols: []string{"WebhookJitterPolicy"},
		IsMutation:    true,
		CreatedAt:     time.Now().UTC(),
	}
	pf, err := tracker.RecordSpan(ctx, repo, prompt1, mutSpan)
	if err != nil {
		t.Fatalf("RecordSpan first mutation: %v", err)
	}
	if pf == nil {
		t.Fatalf("expected non-nil PageFault on first mutation after 4 pre-edit reads")
	}
	if pf.ExtraReadSteps != 4 {
		t.Fatalf("expected ExtraReadSteps=4, got %d", pf.ExtraReadSteps)
	}

	// Subsequent mutations in the same session must not emit duplicate page faults.
	dupPF, err := tracker.RecordSpan(ctx, repo, prompt1, utp.ToolSpan{
		SpanID:        "sp-6",
		SessionID:     session1ID,
		ToolName:      "edit_file",
		TargetFile:    "pkg/webhook/jitter.go",
		TargetSymbols: []string{"BackoffCalculator"},
		IsMutation:    true,
		CreatedAt:     time.Now().UTC(),
	})
	if err != nil || dupPF != nil {
		t.Fatalf("expected nil duplicate PageFault on second mutation, got pf=%+v err=%v", dupPF, err)
	}

	// Verify navigation observations do not pollute PostToolUse hazard SymbolNote when no hazard exists.
	if navLeak, err := st.SymbolNote(ctx, repo, "WebhookJitterPolicy"); err != nil || navLeak != "" {
		t.Fatalf("expected empty PostToolUse hazard note for navigation-only binding, got %q (err=%v)", navLeak, err)
	}

	// Session 2: Immediately asks a similar prompt -> Turn-0 RecallTurnZero hits both target symbols with zero cold grep!
	prompt2 := "fix webhook retry jitter handling for 429 responses"
	warmFrame, err := st.RecallTurnZero(ctx, repo, prompt2, utp.ContextScoped)
	if err != nil {
		t.Fatalf("Session 2 RecallTurnZero: %v", err)
	}
	if len(warmFrame) > sheep.MaxScopedRecallBytes {
		t.Fatalf("warmFrame exceeded %dB cap: %dB", sheep.MaxScopedRecallBytes, len(warmFrame))
	}
	for _, wantSym := range []string{"WebhookJitterPolicy", "BackoffCalculator"} {
		if !strings.Contains(warmFrame, wantSym) {
			t.Fatalf("Session 2 Turn-0 recall missed target symbol %q in frame:\n%s", wantSym, warmFrame)
		}
	}

	// Also verify zero-extra-read PageFault is a clean no-op.
	if err := loops.ProcessPageFault(ctx, st, repo, utp.PageFault{
		SessionID:       "sess-zero",
		PromptSummary:   "noop prompt",
		ResolvedSymbols: []string{"ShouldNotPersist"},
		ExtraReadSteps:  0,
	}); err != nil {
		t.Fatalf("ProcessPageFault(ExtraReadSteps=0): %v", err)
	}
	noopFrame, err := st.RecallTurnZero(ctx, repo, "noop prompt ShouldNotPersist", utp.ContextFull)
	if err != nil {
		t.Fatalf("RecallTurnZero noop check: %v", err)
	}
	if strings.Contains(noopFrame, "ShouldNotPersist") {
		t.Fatalf("ExtraReadSteps=0 should not persist navigation binding, got:\n%s", noopFrame)
	}
}

func TestSymbolRecallInjectorDeduplicationAnd200ByteUTF8Cap(t *testing.T) {
	st, repo := openLoopsTestStore(t)
	ctx := context.Background()

	// 1. Seed a long multi-byte UTF-8 crystallized hazard belief (> 200 bytes raw) on LeaseCoordinator.
	longUTF8Hazard := "LeaseCoordinator ⚠️ must verify fencing token monotonicity — Δt ≤ 250ms — before committing CRDT batch mutations across multi-region Spanner replicas (λ-quorum invariant check required on every write path)"
	for _, sess := range []string{"h1", "h2", "h3"} {
		if err := st.Observe(ctx, repo, sess, "/mem/project", longUTF8Hazard, []string{"LeaseCoordinator"}); err != nil {
			t.Fatalf("Observe LeaseCoordinator: %v", err)
		}
		if err := st.Observe(ctx, repo, sess, "/mem/project",
			"TokenBucket and RateLimiter share burst mutex; acquire RateLimiter.mu before mutating tokens",
			[]string{"TokenBucket", "RateLimiter"},
		); err != nil {
			t.Fatalf("Observe TokenBucket/RateLimiter: %v", err)
		}
	}
	if _, err := st.DreamConsolidate(ctx, repo); err != nil {
		t.Fatalf("DreamConsolidate: %v", err)
	}

	// Also seed a raw observation hazard on CursorSnapshot.
	if err := st.Observe(ctx, repo, "h-raw", "/mem/project",
		"CursorSnapshot must clone underlying WAL slice bytes before page release",
		[]string{"CursorSnapshot"},
	); err != nil {
		t.Fatalf("Observe CursorSnapshot: %v", err)
	}

	inj := loops.NewSymbolRecallInjector(st)

	// Session A, Call 1 on LeaseCoordinator: returns <= 200B valid UTF-8 note.
	note1, err := inj.Inject(ctx, repo, "sess-A", []string{"LeaseCoordinator"})
	if err != nil {
		t.Fatalf("Inject call 1: %v", err)
	}
	if note1 == "" {
		t.Fatalf("expected non-empty note on first touch of LeaseCoordinator")
	}
	if len(note1) > 200 {
		t.Fatalf("Inject note exceeded 200 bytes: got %d bytes (%q)", len(note1), note1)
	}
	if !utf8.ValidString(note1) {
		t.Fatalf("Inject returned invalid UTF-8 string: %q", note1)
	}
	if !strings.Contains(note1, "LeaseCoordinator") {
		t.Fatalf("expected LeaseCoordinator in note: %q", note1)
	}

	// Session A, Call 2 on LeaseCoordinator: suppressed (already delivered in sess-A).
	note2, err := inj.Inject(ctx, repo, "sess-A", []string{"LeaseCoordinator"})
	if err != nil {
		t.Fatalf("Inject call 2: %v", err)
	}
	if note2 != "" {
		t.Fatalf("expected duplicate note suppression in same session, got %q", note2)
	}

	// Session B (different session) on LeaseCoordinator: receives note cleanly!
	noteB, err := inj.Inject(ctx, repo, "sess-B", []string{"LeaseCoordinator"})
	if err != nil {
		t.Fatalf("Inject sess-B: %v", err)
	}
	if noteB == "" || len(noteB) > 200 || !utf8.ValidString(noteB) {
		t.Fatalf("expected valid <=200B note for sess-B, got len=%d %q", len(noteB), noteB)
	}

	// Co-bound symbols (TokenBucket + RateLimiter) in Session A:
	// Touching TokenBucket delivers the shared belief; touching RateLimiter next in Session A suppresses the duplicate!
	coNote1, err := inj.Inject(ctx, repo, "sess-A", []string{"TokenBucket"})
	if err != nil || coNote1 == "" {
		t.Fatalf("expected note for TokenBucket, got %q (err=%v)", coNote1, err)
	}
	coNote2, err := inj.Inject(ctx, repo, "sess-A", []string{"RateLimiter"})
	if err != nil {
		t.Fatalf("Inject RateLimiter: %v", err)
	}
	if coNote2 != "" {
		t.Fatalf("expected co-bound identical note to be deduplicated for RateLimiter, got %q", coNote2)
	}

	// Multi-symbol touch in Session C where two notes exceed 200B combined:
	// First fits <= 200B, and the deferred second symbol note is still delivered on a subsequent touch!
	multi1, err := inj.Inject(ctx, repo, "sess-C", []string{"TokenBucket", "CursorSnapshot"})
	if err != nil {
		t.Fatalf("Inject multi1: %v", err)
	}
	if multi1 == "" || len(multi1) > 200 || !utf8.ValidString(multi1) {
		t.Fatalf("unexpected multi1 (len=%d): %q", len(multi1), multi1)
	}
	if !strings.Contains(multi1, "CursorSnapshot") {
		deferred, err := inj.Inject(ctx, repo, "sess-C", []string{"CursorSnapshot"})
		if err != nil {
			t.Fatalf("Inject deferred CursorSnapshot: %v", err)
		}
		if !strings.Contains(deferred, "CursorSnapshot") || len(deferred) > 200 {
			t.Fatalf("expected deferred CursorSnapshot note on next touch, got %q", deferred)
		}
	}
}

func TestDetectRenamesFromDiffPolyglotAndReconcileASTRenames(t *testing.T) {
	st, repo := openLoopsTestStore(t)
	ctx := context.Background()

	// Seed crystallized beliefs & raw hazard notes across Go, Python, TypeScript, and Rust symbols.
	seedHazards := map[string]string{
		"LegacyDispatcher":      "LegacyDispatcher must drain inflight webhooks before shutdown",
		"DispatchLegacy":        "DispatchLegacy must propagate W3C traceparent header",
		"KeepSameName":          "KeepSameName must preserve idempotency key across retries",
		"DeprecatedPoller":      "DeprecatedPoller polls legacy v0 endpoint every 30s",
		"fetch_legacy_snapshot": "fetch_legacy_snapshot requires read-only Spanner snapshot timestamp",
		"StaleConsolidator":     "StaleConsolidator legacy batch job deprecated in v1",
		"LegacyRouteConfig":     "LegacyRouteConfig must validate confidence threshold in [0,1]",
		"routeLegacyPrompt":     "routeLegacyPrompt must never block >50ms on System 1 classifier",
		"OldWalFrame":           "OldWalFrame requires little-endian CRC32 header framing",
		"verify_old_crc":        "verify_old_crc must reject zero-length corrupted WAL trailers",
		"obsolete_v0_handshake": "obsolete_v0_handshake uses removed v0 wire magic bytes",
	}

	for sym, msg := range seedHazards {
		for _, sess := range []string{"r1", "r2", "r3"} {
			if err := st.Observe(ctx, repo, sess, "/mem/project", msg, []string{sym}); err != nil {
				t.Fatalf("Observe %s: %v", sym, err)
			}
		}
	}
	if _, err := st.DreamConsolidate(ctx, repo); err != nil {
		t.Fatalf("DreamConsolidate: %v", err)
	}

	const polyglotDiff = `diff --git a/pkg/dispatch/dispatcher.go b/pkg/dispatch/dispatcher.go
index 1111111..2222222 100644
--- a/pkg/dispatch/dispatcher.go
+++ b/pkg/dispatch/dispatcher.go
@@ -10,18 +10,14 @@ package dispatch
-type LegacyDispatcher struct {
+type ModernDispatcher struct {
 	queue chan Event
 }
 
-func (d *LegacyDispatcher) DispatchLegacy(ctx context.Context) error {
+func (d *ModernDispatcher) DispatchModern(ctx context.Context) error {
 	return nil
 }
 
-func KeepSameName(ctx context.Context) error {
+func KeepSameName(ctx context.Context, maxRetries int) error {
 	return nil
 }
 
-func DeprecatedPoller(interval time.Duration) {
-	return
-}
diff --git a/src/memory/snapshot.py b/src/memory/snapshot.py
index 3333333..4444444 100644
--- a/src/memory/snapshot.py
+++ b/src/memory/snapshot.py
@@ -4,9 +4,6 @@
-async def fetch_legacy_snapshot(session_id: str) -> dict:
+async def fetch_active_snapshot(session_id: str) -> dict:
     return {"session_id": session_id}
 
-class StaleConsolidator:
-    pass
diff --git a/web/router/config.ts b/web/router/config.ts
index 5555555..6666666 100644
--- a/web/router/config.ts
+++ b/web/router/config.ts
@@ -1,8 +1,8 @@
-export interface LegacyRouteConfig {
+export interface SemanticRouteConfig {
   threshold: number;
 }
 
-export async function routeLegacyPrompt(prompt: string): Promise<string> {
+export async function routeSystemOnePrompt(prompt: string): Promise<string> {
   return prompt;
 }
diff --git a/crates/wal/src/frame.rs b/crates/wal/src/frame.rs
index 7777777..8888888 100644
--- a/crates/wal/src/frame.rs
+++ b/crates/wal/src/frame.rs
@@ -1,12 +1,9 @@
-pub struct OldWalFrame {
+pub struct ChecksumWalFrame {
     pub crc: u32,
 }
 
-pub fn verify_old_crc(buf: &[u8]) -> bool {
+pub fn verify_frame_crc32(buf: &[u8]) -> bool {
     !buf.is_empty()
 }
 
-pub fn obsolete_v0_handshake() {}
`

	detected := loops.DetectRenamesFromDiff(polyglotDiff)

	wantMappings := map[string]string{
		"LegacyDispatcher":      "ModernDispatcher",
		"DispatchLegacy":        "DispatchModern",
		"DeprecatedPoller":      "",
		"fetch_legacy_snapshot": "fetch_active_snapshot",
		"StaleConsolidator":     "",
		"LegacyRouteConfig":     "SemanticRouteConfig",
		"routeLegacyPrompt":     "routeSystemOnePrompt",
		"OldWalFrame":           "ChecksumWalFrame",
		"verify_old_crc":        "verify_frame_crc32",
		"obsolete_v0_handshake": "",
	}
	for oldSym, wantNew := range wantMappings {
		gotNew, ok := detected[oldSym]
		if !ok || gotNew != wantNew {
			t.Fatalf("DetectRenamesFromDiff[%q] = (%q, %v), want (%q, true); full map=%v",
				oldSym, gotNew, ok, wantNew, detected)
		}
	}
	if _, wronglyTouched := detected["KeepSameName"]; wronglyTouched {
		t.Fatalf("KeepSameName only changed parameters and must NOT be evicted/renamed: %v", detected)
	}

	// Reconcile against sheep.Store and verify migrated notes + clean deletions!
	applied, err := loops.ReconcileASTRenames(ctx, st, repo, polyglotDiff)
	if err != nil {
		t.Fatalf("ReconcileASTRenames: %v", err)
	}
	if len(applied) != len(wantMappings) {
		t.Fatalf("expected %d applied symbol changes, got %d (%v)", len(wantMappings), len(applied), applied)
	}

	// 1. Renamed symbols migrate hazard notes to new names and clear old names.
	for oldSym, newSym := range wantMappings {
		oldNote, err := st.SymbolNote(ctx, repo, oldSym)
		if err != nil || oldNote != "" {
			t.Fatalf("expected old/deleted symbol %q note cleared, got %q (err=%v)", oldSym, oldNote, err)
		}
		if newSym != "" {
			migratedNote, err := st.SymbolNote(ctx, repo, newSym)
			if err != nil || migratedNote == "" {
				t.Fatalf("expected migrated hazard note on %q (from %q), got %q (err=%v)", newSym, oldSym, migratedNote, err)
			}
			if !strings.Contains(migratedNote, newSym) {
				t.Fatalf("expected migrated belief text to rewrite %q -> %q, got %q", oldSym, newSym, migratedNote)
			}
		}
	}

	// 2. Deleted symbols are completely purged from RecallTurnZero as well.
	fullRecall, err := st.RecallTurnZero(ctx, repo, "DeprecatedPoller StaleConsolidator obsolete_v0_handshake", utp.ContextFull)
	if err != nil {
		t.Fatalf("RecallTurnZero after reconcile: %v", err)
	}
	for _, deletedSym := range []string{"DeprecatedPoller", "StaleConsolidator", "obsolete_v0_handshake"} {
		if strings.Contains(fullRecall, deletedSym) {
			t.Fatalf("RecallTurnZero leaked deleted symbol %q:\n%s", deletedSym, fullRecall)
		}
	}

	// 3. Unchanged symbol name with modified signature keeps its belief intact.
	keptNote, err := st.SymbolNote(ctx, repo, "KeepSameName")
	if err != nil || !strings.Contains(keptNote, "KeepSameName") {
		t.Fatalf("expected KeepSameName note preserved, got %q (err=%v)", keptNote, err)
	}

	// 4. Also verify before/after symbol-table reconciliation helper.
	beforeTable := loops.ExtractSymbolTable("pkg/auth/verifier.go", `
package auth
type TokenVerifier struct{}
func VerifyBearerToken(raw string) error { return nil }
func LegacySessionCookie(raw string) string { return raw }
`)
	afterTable := loops.ExtractSymbolTable("pkg/auth/verifier.go", `
package auth
type OIDCVerifier struct{}
func VerifyOIDCToken(raw string) error { return nil }
`)
	tableChanges := loops.DetectRenamesFromSymbolTables(beforeTable, afterTable)
	if tableChanges["TokenVerifier"] != "OIDCVerifier" ||
		tableChanges["VerifyBearerToken"] != "VerifyOIDCToken" ||
		tableChanges["LegacySessionCookie"] != "" {
		t.Fatalf("unexpected DetectRenamesFromSymbolTables result: %v", tableChanges)
	}
}
