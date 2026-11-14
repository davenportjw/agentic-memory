package loops

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/davenportjw/toolroll/utp"
)

// NavObservationPrefix marks pre-edit page-fault navigation bindings stored in /mem/project.
const NavObservationPrefix = "[nav] "

var (
	ErrNilStore       = errors.New("sheep/loops: nil memory store")
	ErrEmptySessionID = errors.New("sheep/loops: empty session_id")
)

// Store is the sheep memory store interface consumed by demand loops.
// *sheep.Store satisfies Store structurally without wrappers or import cycles.
type Store interface {
	Observe(ctx context.Context, repoRoot, sessionID, scope, content string, symbols []string) error
	SymbolNote(ctx context.Context, repoRoot, symbol string) (string, error)
	EvictRenamedSymbols(ctx context.Context, repoRoot string, oldToNew map[string]string) error
}

// SessionDeliveryStore is an optional persistence extension implemented by *sheep.Store
// so PostToolUse symbol note deduplication persists across separate CLI hook invocations
// as well as in-memory daemon sessions.
type SessionDeliveryStore interface {
	HasDeliveredNote(ctx context.Context, sessionID, noteKey string) (bool, error)
	MarkDeliveredNote(ctx context.Context, sessionID, noteKey string) error
	ResetDeliveredNotes(ctx context.Context, sessionID string) error
}

var navStopWords = map[string]struct{}{
	"the": {}, "and": {}, "for": {}, "with": {}, "that": {}, "this": {},
	"from": {}, "into": {}, "when": {}, "must": {}, "always": {}, "never": {},
	"are": {}, "was": {}, "were": {}, "have": {}, "has": {}, "had": {},
	"not": {}, "use": {}, "using": {}, "all": {}, "any": {}, "across": {},
	"please": {}, "can": {}, "you": {}, "help": {}, "could": {}, "would": {},
	"should": {}, "where": {}, "what": {}, "which": {}, "how": {}, "why": {},
	"pkg": {}, "src": {}, "cmd": {}, "internal": {}, "lib": {}, "app": {},
	"go": {}, "py": {}, "ts": {}, "tsx": {}, "js": {}, "jsx": {}, "rs": {},
}

type sessionFaultState struct {
	promptSummary   string
	searchedQueries []string
	resolvedSymbols []string
	extraReadSteps  int
	mutated         bool
}

// PageFaultTracker tracks pre-edit read/grep ToolSpans after Turn-0 recall and
// auto-binds missed target symbols into /mem/project on the session's first mutation.
type PageFaultTracker struct {
	mu       sync.Mutex
	store    Store
	sessions map[string]*sessionFaultState
}

// NewPageFaultTracker creates a thread-safe pre-edit PageFaultTracker backed by store.
func NewPageFaultTracker(store Store) *PageFaultTracker {
	return &PageFaultTracker{
		store:    store,
		sessions: make(map[string]*sessionFaultState),
	}
}

// ResetSession clears tracked pre-edit state for sessionID.
func (t *PageFaultTracker) ResetSession(sessionID string) {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return
	}
	t.mu.Lock()
	delete(t.sessions, sessionID)
	t.mu.Unlock()
}

// RecordSpan inspects a utp.ToolSpan for session pre-edit page faults.
// Read/search spans before the first IsMutation==true increment ExtraReadSteps;
// the first IsMutation==true span emits a *utp.PageFault (when ExtraReadSteps > 0)
// and persists a navigation observation via ProcessPageFault.
func (t *PageFaultTracker) RecordSpan(ctx context.Context, repoRoot, promptSummary string, span utp.ToolSpan) (*utp.PageFault, error) {
	sessionID := strings.TrimSpace(span.SessionID)
	if sessionID == "" {
		return nil, ErrEmptySessionID
	}

	var emitted *utp.PageFault

	t.mu.Lock()
	st, ok := t.sessions[sessionID]
	if !ok {
		st = &sessionFaultState{}
		t.sessions[sessionID] = st
	}
	if trimmedPrompt := strings.TrimSpace(promptSummary); trimmedPrompt != "" {
		st.promptSummary = trimmedPrompt
	}

	if !st.mutated {
		if !span.IsMutation {
			if isReadOrSearchSpan(span) {
				st.extraReadSteps++
				if f := strings.TrimSpace(span.TargetFile); f != "" {
					st.searchedQueries = append(st.searchedQueries, f)
				}
				for _, sym := range span.TargetSymbols {
					if s := strings.TrimSpace(sym); s != "" {
						st.resolvedSymbols = append(st.resolvedSymbols, s)
						st.searchedQueries = append(st.searchedQueries, s)
					}
				}
			}
		} else {
			st.mutated = true
			if st.extraReadSteps > 0 {
				combinedSyms := make([]string, 0, len(span.TargetSymbols)+len(st.resolvedSymbols)+1)
				combinedSyms = append(combinedSyms, span.TargetSymbols...)
				combinedSyms = append(combinedSyms, st.resolvedSymbols...)
				resolved := dedupOrderedStrings(combinedSyms)
				if len(resolved) == 0 && strings.TrimSpace(span.TargetFile) != "" {
					resolved = []string{filepath.Base(strings.TrimSpace(span.TargetFile))}
				}

				createdAt := span.CreatedAt
				if createdAt.IsZero() {
					createdAt = time.Now().UTC()
				}
				sum := sha256.Sum256([]byte(fmt.Sprintf("%s|%s|%d|%d", sessionID, st.promptSummary, st.extraReadSteps, createdAt.UnixNano())))
				emitted = &utp.PageFault{
					FaultID:         "pf_" + hex.EncodeToString(sum[:8]),
					SessionID:       sessionID,
					PromptSummary:   st.promptSummary,
					SearchedQueries: dedupOrderedStrings(st.searchedQueries),
					ResolvedSymbols: resolved,
					ExtraReadSteps:  st.extraReadSteps,
					CreatedAt:       createdAt,
				}
			}
		}
	}
	t.mu.Unlock()

	if emitted == nil {
		return nil, nil
	}
	if t.store != nil {
		if err := ProcessPageFault(ctx, t.store, repoRoot, *emitted); err != nil {
			return emitted, err
		}
	}
	return emitted, nil
}

func isReadOrSearchSpan(span utp.ToolSpan) bool {
	name := strings.ToLower(strings.TrimSpace(span.ToolName))
	if name == "" {
		return span.TargetFile != "" || len(span.TargetSymbols) > 0
	}
	for _, kw := range []string{
		"read", "view", "cat", "grep", "rg", "ripgrep", "search",
		"find", "glob", "ls", "list", "outline", "symbol", "refs",
		"blast", "lineage", "ast_", "vfs_",
	} {
		if strings.Contains(name, kw) {
			return true
		}
	}
	return span.TargetFile != "" || len(span.TargetSymbols) > 0
}

// ExtractSalientKeywords extracts ordered, deduplicated domain keywords from a prompt summary
// and pre-edit searched queries/paths, stripping conversational filler and path boilerplate.
func ExtractSalientKeywords(promptSummary string, searchedQueries []string) []string {
	seen := make(map[string]struct{})
	var out []string

	addToken := func(raw string) {
		tok := strings.ToLower(strings.TrimSpace(raw))
		if len(tok) < 2 {
			return
		}
		if _, stop := navStopWords[tok]; stop {
			return
		}
		if _, exists := seen[tok]; exists {
			return
		}
		seen[tok] = struct{}{}
		out = append(out, tok)
	}

	tokenizeText := func(text string, limit int) {
		var cur strings.Builder
		flush := func() {
			if cur.Len() == 0 {
				return
			}
			word := cur.String()
			cur.Reset()
			for _, part := range splitIdentifierParts(word) {
				if len(out) >= limit {
					return
				}
				addToken(part)
			}
		}
		for _, r := range text {
			if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' || r == '-' {
				cur.WriteRune(r)
			} else {
				flush()
				if len(out) >= limit {
					return
				}
			}
		}
		flush()
	}

	tokenizeText(promptSummary, 10)
	for _, q := range searchedQueries {
		if len(out) >= 14 {
			break
		}
		// Strip directory noise when q looks like a file path while keeping stem tokens.
		cleaned := strings.TrimSpace(q)
		if strings.ContainsAny(cleaned, `/\`) {
			ext := filepath.Ext(cleaned)
			base := strings.TrimSuffix(filepath.Base(cleaned), ext)
			dirBase := filepath.Base(filepath.Dir(cleaned))
			if dirBase != "." && dirBase != "/" {
				tokenizeText(dirBase, 14)
			}
			tokenizeText(base, 14)
			continue
		}
		tokenizeText(cleaned, 14)
	}

	return out
}

func splitIdentifierParts(ident string) []string {
	runes := []rune(ident)
	var parts []string
	start := 0
	for i := 1; i < len(runes); i++ {
		prev := runes[i-1]
		curr := runes[i]
		if curr == '_' || curr == '-' {
			if start < i {
				parts = append(parts, string(runes[start:i]))
			}
			start = i + 1
			continue
		}
		if unicode.IsLower(prev) && unicode.IsUpper(curr) {
			parts = append(parts, string(runes[start:i]))
			start = i
		}
	}
	if start < len(runes) {
		parts = append(parts, string(runes[start:]))
	}
	return parts
}

// BuildPageFaultObservation constructs the compact /mem/project navigation observation
// and normalized target symbol list for a pre-edit PageFault.
func BuildPageFaultObservation(pf utp.PageFault) (string, []string) {
	symbols := dedupOrderedStrings(pf.ResolvedSymbols)
	keywords := ExtractSalientKeywords(pf.PromptSummary, pf.SearchedQueries)

	var b strings.Builder
	b.WriteString(NavObservationPrefix)
	if len(keywords) > 0 {
		b.WriteString("intent(")
		b.WriteString(strings.Join(keywords, " "))
		b.WriteString(") -> ")
	}
	b.WriteString("symbols: ")
	b.WriteString(strings.Join(symbols, ", "))
	return b.String(), symbols
}

// ProcessPageFault persists a high-signal navigation observation into /mem/project when
// pf.ExtraReadSteps > 0 so subsequent sessions asking similar prompts recall the exact
// target symbols on Turn 0 without cold grep.
func ProcessPageFault(ctx context.Context, store Store, repoRoot string, pf utp.PageFault) error {
	if store == nil {
		return ErrNilStore
	}
	if pf.ExtraReadSteps <= 0 {
		return nil
	}

	content, symbols := BuildPageFaultObservation(pf)
	if len(symbols) == 0 {
		return errors.New("sheep/loops: page fault requires at least one resolved symbol")
	}

	sessionID := strings.TrimSpace(pf.SessionID)
	if sessionID == "" {
		sessionID = strings.TrimSpace(pf.FaultID)
	}
	if sessionID == "" {
		return ErrEmptySessionID
	}

	return store.Observe(ctx, repoRoot, sessionID, "/mem/project", content, symbols)
}

func dedupOrderedStrings(items []string) []string {
	seen := make(map[string]struct{}, len(items))
	var out []string
	for _, raw := range items {
		s := strings.TrimSpace(raw)
		if s == "" {
			continue
		}
		if _, ok := seen[s]; ok {
			continue
		}
		seen[s] = struct{}{}
		out = append(out, s)
	}
	return out
}
