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

	for _, sub := range []string{"sheep recall", "sheep observe", "sheep note", "sheep dream", "sheep evict", "sheep mounts", "sheep serve"} {
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
		"/api/v1/memory/evict",
	} {
		if !strings.Contains(doc, route) {
			t.Fatalf("docs/sheep-cli.md missing documented HTTP route %q", route)
		}
	}

	// Verify sheep --help documents all 7 subcommands.
	helpOut := execSheep(t, "--help")
	for _, kw := range []string{"recall", "observe", "note", "dream", "evict", "mounts", "serve"} {
		if !strings.Contains(helpOut, kw) {
			t.Fatalf("sheep --help missing %q:\n%s", kw, helpOut)
		}
	}
}
