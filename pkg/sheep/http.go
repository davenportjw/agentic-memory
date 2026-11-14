package sheep

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/davenportjw/sheep/pkg/sheep/loops"
	"github.com/davenportjw/toolroll/utp"
)

const HeaderToolrollSessionID = "X-Toolroll-Session-Id"

// Handler returns a net/http Handler exposing /api/v1/memory/{recall,observe,dream,mounts,note,inject,fault,reconcile-diff,evict}.
func (s *Store) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/api/v1/memory/mounts", s.handleMounts)
	mux.HandleFunc("/api/v1/memory/recall", s.handleRecall)
	mux.HandleFunc("/api/v1/memory/observe", s.handleObserve)
	mux.HandleFunc("/api/v1/memory/dream", s.handleDream)
	mux.HandleFunc("/api/v1/memory/note", s.handleNote)
	mux.HandleFunc("/api/v1/memory/inject", s.handleInject)
	mux.HandleFunc("/api/v1/memory/fault", s.handleFault)
	mux.HandleFunc("/api/v1/memory/reconcile-diff", s.handleReconcileDiff)
	mux.HandleFunc("/api/v1/memory/evict", s.handleEvict)
	return mux
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeJSONError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]any{"error": err.Error()})
}

func (s *Store) handleHealthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "db_path": s.path})
}

func (s *Store) handleMounts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"mounts": s.ListMounts()})
}

func (s *Store) handleRecall(w http.ResponseWriter, r *http.Request) {
	var req struct {
		RepoRoot string           `json:"repo_root"`
		Repo     string           `json:"repo"`
		Intent   string           `json:"intent"`
		Mount    utp.ContextMount `json:"mount"`
	}

	switch r.Method {
	case http.MethodGet:
		q := r.URL.Query()
		req.RepoRoot = q.Get("repo_root")
		if req.RepoRoot == "" {
			req.RepoRoot = q.Get("repo")
		}
		req.Intent = q.Get("intent")
		req.Mount = utp.ContextMount(q.Get("mount"))
	case http.MethodPost:
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	repo := req.RepoRoot
	if repo == "" {
		repo = req.Repo
	}
	mount := req.Mount
	if mount == "" {
		mount = utp.ContextScoped
	}
	frame, err := s.RecallTurnZero(r.Context(), repo, req.Intent, mount)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"frame": frame,
		"mount": mount,
		"bytes": len(frame),
	})
}

func (s *Store) handleObserve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		RepoRoot  string   `json:"repo_root"`
		Repo      string   `json:"repo"`
		SessionID string   `json:"session_id"`
		Scope     string   `json:"scope"`
		Content   string   `json:"content"`
		Symbols   []string `json:"symbols"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	repo := req.RepoRoot
	if repo == "" {
		repo = req.Repo
	}
	sessionID := strings.TrimSpace(req.SessionID)
	if sessionID == "" {
		sessionID = strings.TrimSpace(r.Header.Get(HeaderToolrollSessionID))
	}
	normScope, err := NormalizeScope(req.Scope)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	if err := s.Observe(r.Context(), repo, sessionID, normScope, req.Content, req.Symbols); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "scope": normScope})
}

func (s *Store) handleDream(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		RepoRoot string `json:"repo_root"`
		Repo     string `json:"repo"`
	}
	if r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
	}
	repo := req.RepoRoot
	if repo == "" {
		repo = req.Repo
	}
	promoted, err := s.DreamConsolidate(r.Context(), repo)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"promoted": promoted})
}

func (s *Store) handleNote(w http.ResponseWriter, r *http.Request) {
	var repo, symbol string
	switch r.Method {
	case http.MethodGet:
		q := r.URL.Query()
		repo = q.Get("repo_root")
		if repo == "" {
			repo = q.Get("repo")
		}
		symbol = q.Get("symbol")
	case http.MethodPost:
		var req struct {
			RepoRoot string `json:"repo_root"`
			Repo     string `json:"repo"`
			Symbol   string `json:"symbol"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
		repo = req.RepoRoot
		if repo == "" {
			repo = req.Repo
		}
		symbol = req.Symbol
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	note, err := s.SymbolNote(r.Context(), repo, symbol)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"symbol": symbol, "note": note, "bytes": len(note)})
}

func (s *Store) handleEvict(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		RepoRoot string            `json:"repo_root"`
		Repo     string            `json:"repo"`
		OldToNew map[string]string `json:"old_to_new"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	repo := req.RepoRoot
	if repo == "" {
		repo = req.Repo
	}
	if err := s.EvictRenamedSymbols(r.Context(), repo, req.OldToNew); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "updated": len(req.OldToNew)})
}

func (s *Store) handleInject(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		RepoRoot  string   `json:"repo_root"`
		Repo      string   `json:"repo"`
		SessionID string   `json:"session_id"`
		Symbols   []string `json:"symbols"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	repo := req.RepoRoot
	if repo == "" {
		repo = req.Repo
	}
	sessionID := strings.TrimSpace(req.SessionID)
	if sessionID == "" {
		sessionID = strings.TrimSpace(r.Header.Get(HeaderToolrollSessionID))
	}
	note, err := s.RecallInjector().Inject(r.Context(), repo, sessionID, req.Symbols)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"session_id": sessionID,
		"note":       note,
		"bytes":      len(note),
	})
}

func (s *Store) handleFault(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		RepoRoot        string        `json:"repo_root"`
		Repo            string        `json:"repo"`
		FaultID         string        `json:"fault_id"`
		SessionID       string        `json:"session_id"`
		PromptSummary   string        `json:"prompt_summary"`
		SearchedQueries []string      `json:"searched_queries"`
		ResolvedSymbols []string      `json:"resolved_symbols"`
		ExtraReadSteps  int           `json:"extra_read_steps"`
		Span            *utp.ToolSpan `json:"span,omitempty"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	repo := req.RepoRoot
	if repo == "" {
		repo = req.Repo
	}
	sessionID := strings.TrimSpace(req.SessionID)
	if sessionID == "" {
		sessionID = strings.TrimSpace(r.Header.Get(HeaderToolrollSessionID))
	}

	if req.Span != nil {
		sp := *req.Span
		if strings.TrimSpace(sp.SessionID) == "" {
			sp.SessionID = sessionID
		}
		pf, err := s.FaultTracker().RecordSpan(r.Context(), repo, req.PromptSummary, sp)
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":         true,
			"bound":      pf != nil,
			"page_fault": pf,
		})
		return
	}

	pf := utp.PageFault{
		FaultID:         req.FaultID,
		SessionID:       sessionID,
		PromptSummary:   req.PromptSummary,
		SearchedQueries: req.SearchedQueries,
		ResolvedSymbols: req.ResolvedSymbols,
		ExtraReadSteps:  req.ExtraReadSteps,
	}
	if err := loops.ProcessPageFault(r.Context(), s, repo, pf); err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"bound":            pf.ExtraReadSteps > 0,
		"extra_read_steps": pf.ExtraReadSteps,
		"resolved_symbols": pf.ResolvedSymbols,
	})
}

func (s *Store) handleReconcileDiff(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	ct := strings.ToLower(r.Header.Get("Content-Type"))
	var repo, diffText string
	if strings.Contains(ct, "text/plain") || strings.Contains(ct, "text/x-diff") {
		q := r.URL.Query()
		repo = q.Get("repo_root")
		if repo == "" {
			repo = q.Get("repo")
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
		diffText = string(raw)
	} else {
		var req struct {
			RepoRoot string `json:"repo_root"`
			Repo     string `json:"repo"`
			Diff     string `json:"diff"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
		repo = req.RepoRoot
		if repo == "" {
			repo = req.Repo
		}
		diffText = req.Diff
	}

	changes, err := loops.ReconcileASTRenames(r.Context(), s, repo, diffText)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"old_to_new": changes,
		"updated":    len(changes),
	})
}
