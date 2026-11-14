package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func execSheep(t *testing.T, args ...string) string {
	t.Helper()
	var out, errBuf bytes.Buffer
	if err := runCLI(context.Background(), args, &out, &errBuf); err != nil {
		t.Fatalf("sheep %v failed: %v (stderr: %s)", args, err, errBuf.String())
	}
	return out.String()
}

func TestCLISubcommandsEndToEnd(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "memory.db")
	repo := filepath.Join(dir, "sample-repo")

	// 1. sheep mounts
	mountsOut := execSheep(t, "mounts", "--db", dbPath, "--json")
	var mountsPayload struct {
		Mounts []struct {
			Prefix   string `json:"prefix"`
			ReadOnly bool   `json:"read_only"`
		} `json:"mounts"`
	}
	if err := json.Unmarshal([]byte(mountsOut), &mountsPayload); err != nil {
		t.Fatalf("decode mounts json: %v\nraw: %s", err, mountsOut)
	}
	if len(mountsPayload.Mounts) < 3 {
		t.Fatalf("expected >= 3 default mounts, got %+v", mountsPayload.Mounts)
	}

	// 2. sheep observe across 3 distinct sessions
	for _, sess := range []string{"cli-s1", "cli-s2", "cli-s3"} {
		execSheep(t, "observe",
			"--db", dbPath,
			"--repo", repo,
			"--session", sess,
			"--scope", "/mem/project",
			"--symbols", "SymA,SymB",
			"SymA requires bounded context timeout <= 3s before acquiring SymB lock",
		)
	}

	// 3. sheep note
	noteA := strings.TrimSpace(execSheep(t, "note", "--db", dbPath, "--repo", repo, "SymA"))
	if noteA == "" || len(noteA) > 200 {
		t.Fatalf("unexpected note for SymA (len=%d): %q", len(noteA), noteA)
	}

	// 4. sheep dream
	dreamOut := execSheep(t, "dream", "--db", dbPath, "--repo", repo, "--json")
	var dreamResp struct {
		Promoted int `json:"promoted"`
	}
	if err := json.Unmarshal([]byte(dreamOut), &dreamResp); err != nil {
		t.Fatalf("decode dream json: %v (%s)", err, dreamOut)
	}
	if dreamResp.Promoted != 1 {
		t.Fatalf("expected promoted=1, got %d", dreamResp.Promoted)
	}

	// 5. sheep recall (scoped, full, clean)
	scoped := execSheep(t, "recall", "--db", dbPath, "--repo", repo, "--intent", "SymA timeout lock", "--mount", "scoped")
	if !strings.Contains(scoped, "SymA") || len(scoped) > 2048 {
		t.Fatalf("unexpected scoped recall (len=%d): %q", len(scoped), scoped)
	}

	clean := execSheep(t, "recall", "--db", dbPath, "--repo", repo, "--intent", "SymA timeout lock", "--mount", "clean")
	if clean != "" {
		t.Fatalf("expected empty output for clean recall, got %q", clean)
	}

	// 6. sheep evict (rename SymA=RenamedSymA and delete SymB=)
	execSheep(t, "evict", "--db", dbPath, "--repo", repo, "SymA=RenamedSymA", "SymB=")

	oldNote := strings.TrimSpace(execSheep(t, "note", "--db", dbPath, "--repo", repo, "SymA"))
	if oldNote != "" {
		t.Fatalf("expected empty note for old SymA after rename, got %q", oldNote)
	}
	delNote := strings.TrimSpace(execSheep(t, "note", "--db", dbPath, "--repo", repo, "SymB"))
	if delNote != "" {
		t.Fatalf("expected empty note for deleted SymB, got %q", delNote)
	}
	newNote := strings.TrimSpace(execSheep(t, "note", "--db", dbPath, "--repo", repo, "RenamedSymA"))
	if newNote == "" || len(newNote) > 200 {
		t.Fatalf("expected migrated note for RenamedSymA, got %q", newNote)
	}

	// 7. sheep fault -> immediate Turn-0 scoped recall on next session
	execSheep(t, "fault",
		"--db", dbPath,
		"--repo", repo,
		"--session", "pf-cli-01",
		"--prompt", "resolve quorum lease fencing timeout",
		"--queries", "pkg/lease,quorum",
		"--symbols", "QuorumLeaseManager,FencingValidator",
		"--steps", "4",
		"--json",
	)
	pfRecall := execSheep(t, "recall", "--db", dbPath, "--repo", repo, "--intent", "quorum lease fencing timeout", "--mount", "scoped")
	if !strings.Contains(pfRecall, "QuorumLeaseManager") || !strings.Contains(pfRecall, "FencingValidator") {
		t.Fatalf("expected Turn-0 recall to surface fault-bound symbols, got:\n%s", pfRecall)
	}

	// 8. sheep inject (PostToolUse <=200B note + cross-invocation session deduplication)
	injFirst := strings.TrimSpace(execSheep(t, "inject", "--db", dbPath, "--repo", repo, "--session", "inj-cli-01", "RenamedSymA"))
	if injFirst == "" || len(injFirst) > 200 {
		t.Fatalf("expected <=200B injected note on first touch, got len=%d %q", len(injFirst), injFirst)
	}
	injSecond := strings.TrimSpace(execSheep(t, "inject", "--db", dbPath, "--repo", repo, "--session", "inj-cli-01", "RenamedSymA"))
	if injSecond != "" {
		t.Fatalf("expected empty deduplicated output on repeat inject in same session, got %q", injSecond)
	}

	// 9. sheep reconcile-diff (AST rename + deletion from unified git diff file)
	patchPath := filepath.Join(dir, "rename.diff")
	patchBody := `diff --git a/pkg/lock/sym.go b/pkg/lock/sym.go
--- a/pkg/lock/sym.go
+++ b/pkg/lock/sym.go
@@ -1,5 +1,5 @@
-func RenamedSymA(ctx context.Context) error {
+func FinalSymA(ctx context.Context) error {
 	return nil
 }
`
	if err := os.WriteFile(patchPath, []byte(patchBody), 0o644); err != nil {
		t.Fatalf("write patch file: %v", err)
	}
	recOut := execSheep(t, "reconcile-diff", "--db", dbPath, "--repo", repo, "--diff", patchPath, "--json")
	var recPayload struct {
		OK       bool              `json:"ok"`
		OldToNew map[string]string `json:"old_to_new"`
	}
	if err := json.Unmarshal([]byte(recOut), &recPayload); err != nil {
		t.Fatalf("decode reconcile-diff json: %v (%s)", err, recOut)
	}
	if !recPayload.OK || recPayload.OldToNew["RenamedSymA"] != "FinalSymA" {
		t.Fatalf("unexpected reconcile-diff payload: %+v", recPayload)
	}
	finalNote := strings.TrimSpace(execSheep(t, "note", "--db", dbPath, "--repo", repo, "FinalSymA"))
	if !strings.Contains(finalNote, "FinalSymA") {
		t.Fatalf("expected migrated note on FinalSymA, got %q", finalNote)
	}
}

func TestCLIServeHTTPEndpoints(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "serve-memory.db")
	repo := filepath.Join(dir, "http-repo")

	// Find free loopback port.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen ephemeral: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	errCh := make(chan error, 1)
	go func() {
		var out, errBuf bytes.Buffer
		errCh <- runCLI(ctx, []string{"serve", "--addr", addr, "--db", dbPath}, &out, &errBuf)
	}()

	baseURL := "http://" + addr
	client := &http.Client{Timeout: 2 * time.Second}

	// Wait until server accepts /api/v1/memory/mounts.
	var ready bool
	for i := 0; i < 40; i++ {
		resp, err := client.Get(baseURL + "/api/v1/memory/mounts")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				ready = true
				break
			}
		}
		time.Sleep(15 * time.Millisecond)
	}
	if !ready {
		t.Fatalf("sheep serve did not become ready on %s", baseURL)
	}

	// POST /api/v1/memory/observe across 3 sessions (testing X-Toolroll-Session-Id header too)
	for i := 1; i <= 3; i++ {
		body := fmt.Sprintf(`{"repo_root":%q,"scope":"/mem/project","content":"RouterProxy must preserve X-Turnout-Model SSE headers","symbols":["RouterProxy"]}`, repo)
		req, err := http.NewRequest(http.MethodPost, baseURL+"/api/v1/memory/observe", strings.NewReader(body))
		if err != nil {
			t.Fatalf("new request: %v", err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Toolroll-Session-Id", fmt.Sprintf("http-sess-%d", i))
		resp, err := client.Do(req)
		if err != nil {
			t.Fatalf("POST observe %d: %v", i, err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("POST observe %d status=%d", i, resp.StatusCode)
		}
	}

	// POST /api/v1/memory/dream
	dreamReqBody := fmt.Sprintf(`{"repo_root":%q}`, repo)
	dreamResp, err := client.Post(baseURL+"/api/v1/memory/dream", "application/json", strings.NewReader(dreamReqBody))
	if err != nil {
		t.Fatalf("POST dream: %v", err)
	}
	var dreamJSON struct {
		Promoted int `json:"promoted"`
	}
	if err := json.NewDecoder(dreamResp.Body).Decode(&dreamJSON); err != nil {
		_ = dreamResp.Body.Close()
		t.Fatalf("decode dream response: %v", err)
	}
	_ = dreamResp.Body.Close()
	if dreamJSON.Promoted != 1 {
		t.Fatalf("expected promoted=1 via HTTP, got %d", dreamJSON.Promoted)
	}

	// POST /api/v1/memory/recall
	recallBody := fmt.Sprintf(`{"repo_root":%q,"intent":"RouterProxy SSE headers","mount":"scoped"}`, repo)
	recallResp, err := client.Post(baseURL+"/api/v1/memory/recall", "application/json", strings.NewReader(recallBody))
	if err != nil {
		t.Fatalf("POST recall: %v", err)
	}
	var recallJSON struct {
		Frame string `json:"frame"`
		Bytes int    `json:"bytes"`
	}
	if err := json.NewDecoder(recallResp.Body).Decode(&recallJSON); err != nil {
		_ = recallResp.Body.Close()
		t.Fatalf("decode recall response: %v", err)
	}
	_ = recallResp.Body.Close()
	if !strings.Contains(recallJSON.Frame, "RouterProxy") || recallJSON.Bytes > 2048 {
		t.Fatalf("unexpected HTTP recall: %+v", recallJSON)
	}

	// POST /api/v1/memory/fault -> Turn-0 recall immediately returns fault-bound symbol
	faultBody := fmt.Sprintf(`{"repo_root":%q,"prompt_summary":"calibrate circuit breaker trip threshold","searched_queries":["pkg/proxy","breaker"],"resolved_symbols":["CircuitBreakerPolicy"],"extra_read_steps":3}`, repo)
	faultReq, _ := http.NewRequest(http.MethodPost, baseURL+"/api/v1/memory/fault", strings.NewReader(faultBody))
	faultReq.Header.Set("Content-Type", "application/json")
	faultReq.Header.Set("X-Toolroll-Session-Id", "http-fault-s1")
	faultResp, err := client.Do(faultReq)
	if err != nil {
		t.Fatalf("POST fault: %v", err)
	}
	_ = faultResp.Body.Close()
	if faultResp.StatusCode != http.StatusOK {
		t.Fatalf("POST fault status=%d", faultResp.StatusCode)
	}

	pfRecallBody := fmt.Sprintf(`{"repo_root":%q,"intent":"circuit breaker trip threshold","mount":"scoped"}`, repo)
	pfRecallResp, err := client.Post(baseURL+"/api/v1/memory/recall", "application/json", strings.NewReader(pfRecallBody))
	if err != nil {
		t.Fatalf("POST recall after fault: %v", err)
	}
	var pfRecallJSON struct {
		Frame string `json:"frame"`
	}
	_ = json.NewDecoder(pfRecallResp.Body).Decode(&pfRecallJSON)
	_ = pfRecallResp.Body.Close()
	if !strings.Contains(pfRecallJSON.Frame, "CircuitBreakerPolicy") {
		t.Fatalf("expected HTTP Turn-0 recall to include CircuitBreakerPolicy, got:\n%s", pfRecallJSON.Frame)
	}

	// POST /api/v1/memory/inject (delivers once per session, deduplicates repeat)
	injBody := fmt.Sprintf(`{"repo_root":%q,"symbols":["RouterProxy"]}`, repo)
	for callIdx := 1; callIdx <= 2; callIdx++ {
		injReq, _ := http.NewRequest(http.MethodPost, baseURL+"/api/v1/memory/inject", strings.NewReader(injBody))
		injReq.Header.Set("Content-Type", "application/json")
		injReq.Header.Set("X-Toolroll-Session-Id", "http-inj-sess")
		injResp, err := client.Do(injReq)
		if err != nil {
			t.Fatalf("POST inject %d: %v", callIdx, err)
		}
		var injJSON struct {
			Note  string `json:"note"`
			Bytes int    `json:"bytes"`
		}
		_ = json.NewDecoder(injResp.Body).Decode(&injJSON)
		_ = injResp.Body.Close()
		if callIdx == 1 && (injJSON.Note == "" || injJSON.Bytes > 200) {
			t.Fatalf("expected <=200B note on HTTP inject #1, got %+v", injJSON)
		}
		if callIdx == 2 && injJSON.Note != "" {
			t.Fatalf("expected deduplicated empty note on HTTP inject #2, got %+v", injJSON)
		}
	}

	// POST /api/v1/memory/reconcile-diff
	diffPayload := map[string]string{
		"repo_root": repo,
		"diff": "diff --git a/proxy.go b/proxy.go\n--- a/proxy.go\n+++ b/proxy.go\n@@ -1,3 +1,3 @@\n-type RouterProxy struct{}\n+type SemanticRouterProxy struct{}\n",
	}
	diffBytes, _ := json.Marshal(diffPayload)
	recResp, err := client.Post(baseURL+"/api/v1/memory/reconcile-diff", "application/json", bytes.NewReader(diffBytes))
	if err != nil {
		t.Fatalf("POST reconcile-diff: %v", err)
	}
	var recJSON struct {
		OK       bool              `json:"ok"`
		OldToNew map[string]string `json:"old_to_new"`
	}
	_ = json.NewDecoder(recResp.Body).Decode(&recJSON)
	_ = recResp.Body.Close()
	if !recJSON.OK || recJSON.OldToNew["RouterProxy"] != "SemanticRouterProxy" {
		t.Fatalf("unexpected HTTP reconcile-diff response: %+v", recJSON)
	}

	cancel()
	select {
	case srvErr := <-errCh:
		if srvErr != nil {
			t.Fatalf("sheep serve exit error: %v", srvErr)
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("sheep serve did not shut down within 2s")
	}
}

func TestDocExamplesParity(t *testing.T) {
	docBytes, err := os.ReadFile(filepath.Join("..", "..", "docs", "sheep-cli.md"))
	if err != nil {
		t.Fatalf("read docs/sheep-cli.md: %v", err)
	}
	doc := string(docBytes)

	for _, sub := range []string{
		"sheep recall", "sheep observe", "sheep note", "sheep inject",
		"sheep fault", "sheep reconcile-diff", "sheep dream", "sheep evict",
		"sheep mounts", "sheep serve",
	} {
		if !strings.Contains(doc, sub) {
			t.Fatalf("docs/sheep-cli.md missing documented subcommand %q", sub)
		}
	}
	for _, route := range []string{
		"/api/v1/memory/mounts",
		"/api/v1/memory/recall",
		"/api/v1/memory/observe",
		"/api/v1/memory/dream",
		"/api/v1/memory/note",
		"/api/v1/memory/inject",
		"/api/v1/memory/fault",
		"/api/v1/memory/reconcile-diff",
		"/api/v1/memory/evict",
	} {
		if !strings.Contains(doc, route) {
			t.Fatalf("docs/sheep-cli.md missing documented HTTP route %q", route)
		}
	}

	// Verify sheep --help documents all subcommands.
	helpOut := execSheep(t, "--help")
	for _, kw := range []string{"recall", "observe", "note", "inject", "fault", "reconcile-diff", "dream", "evict", "mounts", "serve"} {
		if !strings.Contains(helpOut, kw) {
			t.Fatalf("sheep --help missing %q:\n%s", kw, helpOut)
		}
	}
}
