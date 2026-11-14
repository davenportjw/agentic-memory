package loops

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"
)

// MaxPostToolNoteBytes enforces the strict <= 200 byte cap on PostToolUse symbol recall injections.
const MaxPostToolNoteBytes = 200

// SymbolRecallInjector delivers <= 200 byte UTF-8-safe symbol hazard notes on PostToolUse
// while deduplicating notes already surfaced earlier in the same sessionID.
type SymbolRecallInjector struct {
	mu        sync.Mutex
	store     Store
	delivered map[string]map[string]struct{} // sessionID -> set(noteKey)
}

// NewSymbolRecallInjector constructs a session-deduplicating PostToolUse symbol recall injector.
func NewSymbolRecallInjector(store Store) *SymbolRecallInjector {
	return &SymbolRecallInjector{
		store:     store,
		delivered: make(map[string]map[string]struct{}),
	}
}

// ResetSession clears delivered note tracking for sessionID in memory (and in SQLite if supported).
func (inj *SymbolRecallInjector) ResetSession(ctx context.Context, sessionID string) error {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return nil
	}
	inj.mu.Lock()
	delete(inj.delivered, sessionID)
	inj.mu.Unlock()

	if ds, ok := inj.store.(SessionDeliveryStore); ok {
		return ds.ResetDeliveredNotes(ctx, sessionID)
	}
	return nil
}

// Inject queries Store.SymbolNote across touched/read symbols, skips any notes already
// delivered to sessionID in this session, and packs undelivered notes into a single
// compact <= 200 byte UTF-8-safe string.
func (inj *SymbolRecallInjector) Inject(ctx context.Context, repoRoot, sessionID string, symbols []string) (string, error) {
	if inj == nil || inj.store == nil {
		return "", ErrNilStore
	}

	normRepo := strings.TrimSpace(repoRoot)
	if normRepo == "" {
		normRepo = "."
	} else {
		normRepo = filepath.Clean(normRepo)
	}
	sid := strings.TrimSpace(sessionID)
	orderedSyms := dedupOrderedStrings(symbols)
	if len(orderedSyms) == 0 {
		return "", nil
	}

	type candidateNote struct {
		noteKey   string
		formatted string
	}

	seenInBatch := make(map[string]struct{}, len(orderedSyms))
	var pending []candidateNote

	for _, sym := range orderedSyms {
		rawNote, err := inj.store.SymbolNote(ctx, normRepo, sym)
		if err != nil {
			return "", err
		}
		rawNote = strings.TrimSpace(rawNote)
		if rawNote == "" {
			continue
		}

		noteKey := hashNoteKey(normRepo, rawNote)
		if _, dupBatch := seenInBatch[noteKey]; dupBatch {
			continue
		}

		alreadySent, err := inj.wasDelivered(ctx, sid, noteKey)
		if err != nil {
			return "", err
		}
		if alreadySent {
			continue
		}

		seenInBatch[noteKey] = struct{}{}
		formatted := rawNote
		if !strings.Contains(rawNote, sym) {
			formatted = sym + ": " + rawNote
		}
		pending = append(pending, candidateNote{
			noteKey:   noteKey,
			formatted: formatted,
		})
	}

	if len(pending) == 0 {
		return "", nil
	}

	// Pack the first note cleanly within <= 200 UTF-8 bytes; append additional notes
	// separated by " | " only when they fit cleanly inside MaxPostToolNoteBytes so
	// overflow notes remain undelivered and surface on their next touch.
	first := clampUTF8Bytes(pending[0].formatted, MaxPostToolNoteBytes)
	if err := inj.markDelivered(ctx, sid, pending[0].noteKey); err != nil {
		return "", err
	}

	out := first
	for i := 1; i < len(pending); i++ {
		sep := " | "
		if len(out)+len(sep)+len(pending[i].formatted) <= MaxPostToolNoteBytes {
			out += sep + pending[i].formatted
			if err := inj.markDelivered(ctx, sid, pending[i].noteKey); err != nil {
				return "", err
			}
		}
	}

	return clampUTF8Bytes(out, MaxPostToolNoteBytes), nil
}

func (inj *SymbolRecallInjector) wasDelivered(ctx context.Context, sessionID, noteKey string) (bool, error) {
	if sessionID == "" {
		return false, nil
	}
	inj.mu.Lock()
	if set, ok := inj.delivered[sessionID]; ok {
		if _, exists := set[noteKey]; exists {
			inj.mu.Unlock()
			return true, nil
		}
	}
	inj.mu.Unlock()

	if ds, ok := inj.store.(SessionDeliveryStore); ok {
		delivered, err := ds.HasDeliveredNote(ctx, sessionID, noteKey)
		if err != nil {
			return false, err
		}
		if delivered {
			inj.mu.Lock()
			if inj.delivered[sessionID] == nil {
				inj.delivered[sessionID] = make(map[string]struct{})
			}
			inj.delivered[sessionID][noteKey] = struct{}{}
			inj.mu.Unlock()
			return true, nil
		}
	}
	return false, nil
}

func (inj *SymbolRecallInjector) markDelivered(ctx context.Context, sessionID, noteKey string) error {
	if sessionID == "" {
		return nil
	}
	inj.mu.Lock()
	if inj.delivered[sessionID] == nil {
		inj.delivered[sessionID] = make(map[string]struct{})
	}
	inj.delivered[sessionID][noteKey] = struct{}{}
	inj.mu.Unlock()

	if ds, ok := inj.store.(SessionDeliveryStore); ok {
		if err := ds.MarkDeliveredNote(ctx, sessionID, noteKey); err != nil {
			return err
		}
	}
	return nil
}

func hashNoteKey(repoRoot, note string) string {
	sum := sha256.Sum256([]byte(repoRoot + "\x00" + note))
	return hex.EncodeToString(sum[:16])
}

func clampUTF8Bytes(s string, maxBytes int) string {
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
