package loops

import (
	"context"
	"math"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// SymbolDecl describes a parsed function or type declaration across Go, Python, TypeScript, or Rust.
type SymbolDecl struct {
	Name      string
	Kind      string // "func" | "type"
	Language  string // "go" | "python" | "typescript" | "rust"
	File      string
	Signature string
	HunkID    int
	Line      int
}

var (
	// Go declarations
	reGoMethod = regexp.MustCompile(`^func\s*\([^)]+\)\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*(\(.*)`)
	reGoFunc   = regexp.MustCompile(`^func\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*(\(.*)`)
	reGoType   = regexp.MustCompile(`^type\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s+(.+)`)

	// Python declarations
	rePyDef   = regexp.MustCompile(`^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\(.*)`)
	rePyClass = regexp.MustCompile(`^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)`)

	// TypeScript / JavaScript declarations
	reTSFunc  = regexp.MustCompile(`^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:<[^>]*>)?\s*(\(.*)`)
	reTSArrow = regexp.MustCompile(`^(?:export\s+)?(?:const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(\([^)]*\)\s*(?::[^=]+)?=>.*)`)
	reTSType  = regexp.MustCompile(`^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:declare\s+)?(class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(.*)`)

	// Rust declarations
	reRustFn   = regexp.MustCompile(`^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?(?:extern\s+(?:"[^"]*"\s+)?)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*(\(.*)`)
	reRustType = regexp.MustCompile(`^(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|type)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)`)
)

func detectLanguageFromPath(filePath string) string {
	ext := strings.ToLower(filepath.Ext(strings.TrimSpace(filePath)))
	switch ext {
	case ".go":
		return "go"
	case ".py", ".pyi":
		return "python"
	case ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts":
		return "typescript"
	case ".rs":
		return "rust"
	default:
		return ""
	}
}

func normalizeSignature(sig string) string {
	s := strings.TrimSpace(sig)
	// Strip trailing inline comments for cleaner structural comparison.
	if idx := strings.Index(s, "//"); idx >= 0 {
		s = strings.TrimSpace(s[:idx])
	}
	return strings.Join(strings.Fields(s), " ")
}

func parseDeclarationLine(lang, file, rawLine string, hunkID, lineNo int) (SymbolDecl, bool) {
	trimmed := strings.TrimSpace(rawLine)
	if trimmed == "" ||
		strings.HasPrefix(trimmed, "//") ||
		strings.HasPrefix(trimmed, "#") ||
		strings.HasPrefix(trimmed, "/*") ||
		strings.HasPrefix(trimmed, "*") {
		return SymbolDecl{}, false
	}

	tryGo := func() (SymbolDecl, bool) {
		if m := reGoMethod.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "func", Language: "go",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		if m := reGoFunc.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "func", Language: "go",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		if m := reGoType.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "type", Language: "go",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		return SymbolDecl{}, false
	}

	tryPython := func() (SymbolDecl, bool) {
		if m := rePyDef.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "func", Language: "python",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		if m := rePyClass.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "type", Language: "python",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		return SymbolDecl{}, false
	}

	tryTS := func() (SymbolDecl, bool) {
		if m := reTSFunc.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "func", Language: "typescript",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		if m := reTSArrow.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "func", Language: "typescript",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		if m := reTSType.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[2], Kind: "type", Language: "typescript",
				File: file, Signature: normalizeSignature(m[1] + " " + m[3]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		return SymbolDecl{}, false
	}

	tryRust := func() (SymbolDecl, bool) {
		if m := reRustFn.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[1], Kind: "func", Language: "rust",
				File: file, Signature: normalizeSignature(m[2]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		if m := reRustType.FindStringSubmatch(trimmed); m != nil {
			return SymbolDecl{
				Name: m[2], Kind: "type", Language: "rust",
				File: file, Signature: normalizeSignature(m[1] + " " + m[3]), HunkID: hunkID, Line: lineNo,
			}, true
		}
		return SymbolDecl{}, false
	}

	switch lang {
	case "go":
		return tryGo()
	case "python":
		return tryPython()
	case "typescript":
		return tryTS()
	case "rust":
		return tryRust()
	default:
		if d, ok := tryGo(); ok {
			return d, true
		}
		if d, ok := tryPython(); ok {
			return d, true
		}
		if d, ok := tryRust(); ok {
			return d, true
		}
		return tryTS()
	}
}

// ExtractSymbolTable extracts top-level and method declarations from a source file
// across Go, Python, TypeScript/JavaScript, and Rust.
func ExtractSymbolTable(filePath, source string) []SymbolDecl {
	lang := detectLanguageFromPath(filePath)
	normFile := normalizeDiffPath(filePath)
	lines := strings.Split(source, "\n")
	var out []SymbolDecl
	for idx, line := range lines {
		if decl, ok := parseDeclarationLine(lang, normFile, line, 0, idx+1); ok {
			out = append(out, decl)
		}
	}
	return out
}

// DetectRenamesFromSymbolTables compares before/after symbol tables and returns
// a map of OldSym -> NewSym (for renames) and OldSym -> "" (for deleted symbols).
func DetectRenamesFromSymbolTables(before, after []SymbolDecl) map[string]string {
	return reconcileRemovedAndAdded(before, after)
}

// DetectRenamesFromDiff parses a unified git diff across Go, Python, TypeScript, and Rust
// to identify symbol renames (OldSym -> NewSym) and deletions (OldSym -> "").
func DetectRenamesFromDiff(diffText string) map[string]string {
	var removed, added []SymbolDecl
	var curFile, curLang string
	hunkSeq := 0
	lineSeq := 0

	for _, rawLine := range strings.Split(diffText, "\n") {
		lineSeq++
		if strings.HasPrefix(rawLine, "diff --git ") {
			parts := strings.Fields(rawLine)
			if len(parts) >= 4 {
				curFile = normalizeDiffPath(parts[3])
				curLang = detectLanguageFromPath(curFile)
			}
			continue
		}
		if strings.HasPrefix(rawLine, "--- ") {
			p := normalizeDiffPath(strings.TrimSpace(strings.TrimPrefix(rawLine, "--- ")))
			if p != "" && p != "/dev/null" {
				curFile = p
				if l := detectLanguageFromPath(p); l != "" {
					curLang = l
				}
			}
			continue
		}
		if strings.HasPrefix(rawLine, "+++ ") {
			p := normalizeDiffPath(strings.TrimSpace(strings.TrimPrefix(rawLine, "+++ ")))
			if p != "" && p != "/dev/null" {
				curFile = p
				if l := detectLanguageFromPath(p); l != "" {
					curLang = l
				}
			}
			continue
		}
		if strings.HasPrefix(rawLine, "@@ ") {
			hunkSeq++
			continue
		}

		if strings.HasPrefix(rawLine, "-") && !strings.HasPrefix(rawLine, "---") {
			if decl, ok := parseDeclarationLine(curLang, curFile, rawLine[1:], hunkSeq, lineSeq); ok {
				removed = append(removed, decl)
			}
			continue
		}
		if strings.HasPrefix(rawLine, "+") && !strings.HasPrefix(rawLine, "+++") {
			if decl, ok := parseDeclarationLine(curLang, curFile, rawLine[1:], hunkSeq, lineSeq); ok {
				added = append(added, decl)
			}
			continue
		}
	}

	return reconcileRemovedAndAdded(removed, added)
}

// ReconcileASTRenames parses diffText for AST function/type renames and deletions,
// applies them to store via EvictRenamedSymbols, and returns the applied mapping.
func ReconcileASTRenames(ctx context.Context, store Store, repoRoot, diffText string) (map[string]string, error) {
	if store == nil {
		return nil, ErrNilStore
	}
	changes := DetectRenamesFromDiff(diffText)
	if len(changes) == 0 {
		return map[string]string{}, nil
	}
	if err := store.EvictRenamedSymbols(ctx, repoRoot, changes); err != nil {
		return nil, err
	}
	return changes, nil
}

// ReconcileSymbolTables diffs before/after symbol tables, applies renames and deletions
// via EvictRenamedSymbols, and returns the applied mapping.
func ReconcileSymbolTables(ctx context.Context, store Store, repoRoot string, before, after []SymbolDecl) (map[string]string, error) {
	if store == nil {
		return nil, ErrNilStore
	}
	changes := DetectRenamesFromSymbolTables(before, after)
	if len(changes) == 0 {
		return map[string]string{}, nil
	}
	if err := store.EvictRenamedSymbols(ctx, repoRoot, changes); err != nil {
		return nil, err
	}
	return changes, nil
}

func normalizeDiffPath(p string) string {
	p = strings.TrimSpace(p)
	if p == "" || p == "/dev/null" {
		return p
	}
	// Strip git a/ or b/ prefixes.
	if strings.HasPrefix(p, "a/") || strings.HasPrefix(p, "b/") {
		p = p[2:]
	}
	return filepath.ToSlash(filepath.Clean(p))
}

func reconcileRemovedAndAdded(removed, added []SymbolDecl) map[string]string {
	changes := make(map[string]string)
	if len(removed) == 0 {
		return changes
	}

	// Pass 0: Eliminate symbols present by the same name in both removed and added
	// (e.g. parameter signature edits or pure cross-file moves without rename).
	addedNames := make(map[string]struct{}, len(added))
	for _, a := range added {
		addedNames[a.Name] = struct{}{}
	}
	removedNames := make(map[string]struct{}, len(removed))
	for _, r := range removed {
		removedNames[r.Name] = struct{}{}
	}

	var unmatchedRem []SymbolDecl
	seenRem := make(map[string]struct{})
	for _, r := range removed {
		if _, kept := addedNames[r.Name]; kept {
			continue
		}
		if _, dup := seenRem[r.Name]; dup {
			continue
		}
		seenRem[r.Name] = struct{}{}
		unmatchedRem = append(unmatchedRem, r)
	}

	var unmatchedAdd []SymbolDecl
	seenAdd := make(map[string]struct{})
	for _, a := range added {
		if _, kept := removedNames[a.Name]; kept {
			continue
		}
		if _, dup := seenAdd[a.Name]; dup {
			continue
		}
		seenAdd[a.Name] = struct{}{}
		unmatchedAdd = append(unmatchedAdd, a)
	}

	if len(unmatchedRem) == 0 {
		return changes
	}

	// Pass 1: Score compatible (removed, added) candidate pairs and match greedily
	// above the rename confidence floor (0.55).
	type candidatePair struct {
		remIdx int
		addIdx int
		score  float64
	}
	var pairs []candidatePair
	for ri, r := range unmatchedRem {
		for ai, a := range unmatchedAdd {
			if r.Kind != a.Kind {
				continue
			}
			if r.Language != "" && a.Language != "" && r.Language != a.Language {
				continue
			}
			sc := scoreRenamePair(r, a)
			if sc >= 0.55 {
				pairs = append(pairs, candidatePair{remIdx: ri, addIdx: ai, score: sc})
			}
		}
	}

	sort.SliceStable(pairs, func(i, j int) bool {
		if math.Abs(pairs[i].score-pairs[j].score) > 1e-9 {
			return pairs[i].score > pairs[j].score
		}
		if pairs[i].remIdx != pairs[j].remIdx {
			return pairs[i].remIdx < pairs[j].remIdx
		}
		return pairs[i].addIdx < pairs[j].addIdx
	})

	usedRem := make([]bool, len(unmatchedRem))
	usedAdd := make([]bool, len(unmatchedAdd))
	for _, p := range pairs {
		if usedRem[p.remIdx] || usedAdd[p.addIdx] {
			continue
		}
		usedRem[p.remIdx] = true
		usedAdd[p.addIdx] = true
		changes[unmatchedRem[p.remIdx].Name] = unmatchedAdd[p.addIdx].Name
	}

	// Pass 2: Any remaining unmatched removed symbol was deleted (OldSym -> "").
	for ri, r := range unmatchedRem {
		if !usedRem[ri] {
			changes[r.Name] = ""
		}
	}

	return changes
}

func scoreRenamePair(rem, add SymbolDecl) float64 {
	score := 0.0

	if rem.Signature != "" && rem.Signature == add.Signature {
		score += 0.70
	} else {
		score += 0.45 * tokenSetOverlap(rem.Signature, add.Signature)
	}

	score += 0.35 * identifierStemOverlap(rem.Name, add.Name)

	if rem.File != "" && rem.File == add.File {
		score += 0.20
		if rem.HunkID > 0 && rem.HunkID == add.HunkID {
			score += 0.35
			dist := math.Abs(float64(rem.Line - add.Line))
			score += 0.15 / (1.0 + dist)
		}
	}

	return score
}

func identifierStemOverlap(a, b string) float64 {
	partsA := splitIdentifierParts(a)
	partsB := splitIdentifierParts(b)
	if len(partsA) == 0 || len(partsB) == 0 {
		return 0
	}
	setA := make(map[string]struct{}, len(partsA))
	for _, p := range partsA {
		lp := strings.ToLower(p)
		if len(lp) >= 2 {
			setA[lp] = struct{}{}
		}
	}
	setB := make(map[string]struct{}, len(partsB))
	for _, p := range partsB {
		lp := strings.ToLower(p)
		if len(lp) >= 2 {
			setB[lp] = struct{}{}
		}
	}
	if len(setA) == 0 || len(setB) == 0 {
		return 0
	}
	matches := 0
	for k := range setA {
		if _, ok := setB[k]; ok {
			matches++
		}
	}
	if matches == 0 {
		return 0
	}
	return float64(matches) / math.Sqrt(float64(len(setA))*float64(len(setB)))
}

func tokenSetOverlap(a, b string) float64 {
	toksA := simpleSigTokens(a)
	toksB := simpleSigTokens(b)
	if len(toksA) == 0 || len(toksB) == 0 {
		return 0
	}
	matches := 0
	for k := range toksA {
		if _, ok := toksB[k]; ok {
			matches++
		}
	}
	if matches == 0 {
		return 0
	}
	return float64(matches) / math.Sqrt(float64(len(toksA))*float64(len(toksB)))
}

func simpleSigTokens(sig string) map[string]struct{} {
	out := make(map[string]struct{})
	var cur strings.Builder
	flush := func() {
		if cur.Len() > 0 {
			out[strings.ToLower(cur.String())] = struct{}{}
			cur.Reset()
		}
	}
	for _, r := range sig {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' {
			cur.WriteRune(r)
		} else {
			flush()
		}
	}
	flush()
	return out
}
