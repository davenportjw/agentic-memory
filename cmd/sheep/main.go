package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/davenportjw/electric-sheep/pkg/sheep"
	"github.com/davenportjw/electric-sheep/pkg/sheep/loops"
	"github.com/davenportjw/toolroll/utp"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := runCLI(ctx, os.Args[1:], os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "sheep: %v\n", err)
		os.Exit(1)
	}
}

func runCLI(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		printUsage(stderr)
		return errors.New("subcommand required")
	}

	switch args[0] {
	case "-h", "--help", "help":
		printUsage(stdout)
		return nil
	case "recall":
		return runRecall(ctx, args[1:], stdout, stderr)
	case "observe":
		return runObserve(ctx, args[1:], stdout, stderr)
	case "note":
		return runNote(ctx, args[1:], stdout, stderr)
	case "inject":
		return runInject(ctx, args[1:], stdout, stderr)
	case "fault":
		return runFault(ctx, args[1:], stdout, stderr)
	case "reconcile-diff":
		return runReconcileDiff(ctx, args[1:], stdout, stderr)
	case "dream":
		return runDream(ctx, args[1:], stdout, stderr)
	case "evict":
		return runEvict(ctx, args[1:], stdout, stderr)
	case "mounts":
		return runMounts(ctx, args[1:], stdout, stderr)
	case "serve":
		return runServe(ctx, args[1:], stdout, stderr)
	default:
		printUsage(stderr)
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

func printUsage(w io.Writer) {
	fmt.Fprint(w, `sheep — Demand-Driven Agent Memory CLI (Toolroll / Electric Sheep)

Usage:
  sheep recall         --repo <dir> --intent "..." [--mount scoped|full|clean] [--db <path>] [--json]
  sheep observe        --repo <dir> --session <id> --scope /mem/project [--symbols SymA,SymB] [--db <path>] "..."
  sheep note           --repo <dir> [--db <path>] <Symbol>
  sheep inject         --repo <dir> --session <id> [--db <path>] [--json] <SymA> [SymB...]
  sheep fault          --repo <dir> --session <id> --prompt "..." [--queries q1,q2] --symbols SymA,SymB [--steps 1] [--db <path>] [--json]
  sheep reconcile-diff --repo <dir> [--diff <patch-or-stdin>] [--db <path>] [--json]
  sheep dream          --repo <dir> [--db <path>] [--json]
  sheep evict          --repo <dir> [--db <path>] OldSym=NewSym [DeletedSym= ...]
  sheep mounts         [--db <path>] [--json]
  sheep serve          [--addr 127.0.0.1:7410] [--db <path>]
`)
}

// hoistFlags moves flag arguments ahead of positional arguments so flags work in any position.
func hoistFlags(args []string, boolFlags map[string]bool) []string {
	var flagsPart, posPart []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			posPart = append(posPart, args[i+1:]...)
			break
		}
		if strings.HasPrefix(a, "-") && len(a) > 1 {
			flagsPart = append(flagsPart, a)
			name := strings.TrimLeft(a, "-")
			if !strings.Contains(name, "=") && !boolFlags[name] && i+1 < len(args) {
				flagsPart = append(flagsPart, args[i+1])
				i++
			}
		} else {
			posPart = append(posPart, a)
		}
	}
	return append(flagsPart, posPart...)
}

func openStoreFromFlags(dbPath string) (*sheep.Store, error) {
	cfg := sheep.Config{DBPath: dbPath}
	if inst := os.Getenv("SHEEP_SPANNER_INSTANCE"); inst != "" {
		cfg.Spanner = &sheep.SpannerConfig{
			ProjectID:  os.Getenv("GOOGLE_CLOUD_PROJECT"),
			InstanceID: inst,
			DatabaseID: os.Getenv("SHEEP_SPANNER_DATABASE"),
		}
	}
	return sheep.OpenWithConfig(cfg)
}

func runRecall(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("recall", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	intent := fs.String("intent", "", "Turn-0 user intent or query")
	mount := fs.String("mount", string(utp.ContextScoped), "Context mount: scoped | full | clean")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}
	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	frame, err := st.RecallTurnZero(ctx, *repo, *intent, utp.ContextMount(strings.TrimSpace(*mount)))
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{
			"frame": frame,
			"mount": *mount,
			"bytes": len(frame),
		})
	}
	if frame != "" {
		_, _ = io.WriteString(stdout, frame)
		if !strings.HasSuffix(frame, "\n") {
			_, _ = io.WriteString(stdout, "\n")
		}
	}
	return nil
}

func runObserve(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("observe", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	session := fs.String("session", os.Getenv("TOOLROLL_SESSION_ID"), "Session ID")
	scope := fs.String("scope", "/mem/project", "Memory scope: /mem/personal | /mem/project | /mem/org")
	symbolsRaw := fs.String("symbols", "", "Comma-separated AST symbol bindings")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}
	content := strings.TrimSpace(strings.Join(fs.Args(), " "))
	if content == "" {
		return errors.New("observation text argument required")
	}

	var symbols []string
	if strings.TrimSpace(*symbolsRaw) != "" {
		symbols = strings.Split(*symbolsRaw, ",")
	}

	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	normScope, err := sheep.NormalizeScope(*scope)
	if err != nil {
		return err
	}
	if err := st.Observe(ctx, *repo, *session, normScope, content, symbols); err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{"ok": true, "scope": normScope})
	}
	fmt.Fprintf(stdout, "observed (%s)\n", normScope)
	return nil
}

func runNote(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("note", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return errors.New("symbol argument required: sheep note --repo <dir> <Symbol>")
	}
	symbol := fs.Arg(0)

	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	note, err := st.SymbolNote(ctx, *repo, symbol)
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{"symbol": symbol, "note": note, "bytes": len(note)})
	}
	if note != "" {
		fmt.Fprintln(stdout, note)
	}
	return nil
}

func runDream(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("dream", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}
	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	promoted, err := st.DreamConsolidate(ctx, *repo)
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{"promoted": promoted})
	}
	fmt.Fprintf(stdout, "promoted: %d\n", promoted)
	return nil
}

func runEvict(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("evict", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return errors.New("at least one OldSym=NewSym or DeletedSym= mapping required")
	}

	oldToNew := make(map[string]string, fs.NArg())
	for _, pair := range fs.Args() {
		idx := strings.IndexByte(pair, '=')
		if idx <= 0 {
			return fmt.Errorf("invalid symbol mapping %q (expected OldSym=NewSym or DeletedSym=)", pair)
		}
		oldSym := strings.TrimSpace(pair[:idx])
		newSym := strings.TrimSpace(pair[idx+1:])
		oldToNew[oldSym] = newSym
	}

	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	if err := st.EvictRenamedSymbols(ctx, *repo, oldToNew); err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{"ok": true, "updated": len(oldToNew)})
	}
	fmt.Fprintf(stdout, "evicted/renamed: %d\n", len(oldToNew))
	return nil
}

func runMounts(_ context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("mounts", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}
	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	mounts := st.ListMounts()
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{"mounts": mounts})
	}
	for _, m := range mounts {
		mode := "rw"
		if m.ReadOnly {
			mode = "ro"
		}
		fmt.Fprintf(stdout, "%-16s %-26s %-4s %s\n", m.Prefix, m.Backend, mode, m.Description)
	}
	return nil
}

func runServe(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	fs.SetOutput(stderr)
	addr := fs.String("addr", "127.0.0.1:7410", "TCP listen address")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")

	if err := fs.Parse(hoistFlags(args, nil)); err != nil {
		return err
	}
	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		return err
	}
	srv := &http.Server{
		Handler:           st.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		fmt.Fprintf(stdout, "sheep listening on http://%s (db=%s)\n", ln.Addr().String(), st.Path())
		if serveErr := srv.Serve(ln); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			errCh <- serveErr
			return
		}
		errCh <- nil
	}()

	select {
	case <-ctx.Done():
		shutCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutCtx)
		return <-errCh
	case serveErr := <-errCh:
		return serveErr
	}
}

func runInject(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("inject", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	session := fs.String("session", os.Getenv("TOOLROLL_SESSION_ID"), "Session ID for deduplicating delivered symbol notes")
	symbolsRaw := fs.String("symbols", "", "Optional comma-separated symbol list")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}

	var symbols []string
	if strings.TrimSpace(*symbolsRaw) != "" {
		symbols = append(symbols, strings.Split(*symbolsRaw, ",")...)
	}
	symbols = append(symbols, fs.Args()...)
	if len(symbols) == 0 {
		return errors.New("at least one symbol required: sheep inject --repo <dir> --session <id> <SymA> [SymB...]")
	}

	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	note, err := st.RecallInjector().Inject(ctx, *repo, *session, symbols)
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{
			"session_id": *session,
			"note":       note,
			"bytes":      len(note),
		})
	}
	if note != "" {
		fmt.Fprintln(stdout, note)
	}
	return nil
}

func runFault(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("fault", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	session := fs.String("session", os.Getenv("TOOLROLL_SESSION_ID"), "Session ID")
	prompt := fs.String("prompt", "", "User prompt summary that triggered pre-edit page fault")
	queriesRaw := fs.String("queries", "", "Comma-separated searched queries or file paths")
	symbolsRaw := fs.String("symbols", "", "Comma-separated resolved target symbols")
	steps := fs.Int("steps", 1, "Extra pre-edit read/grep steps")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}

	var queries, symbols []string
	if strings.TrimSpace(*queriesRaw) != "" {
		queries = strings.Split(*queriesRaw, ",")
	}
	if strings.TrimSpace(*symbolsRaw) != "" {
		symbols = strings.Split(*symbolsRaw, ",")
	}
	symbols = append(symbols, fs.Args()...)

	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	pf := utp.PageFault{
		SessionID:       *session,
		PromptSummary:   *prompt,
		SearchedQueries: queries,
		ResolvedSymbols: symbols,
		ExtraReadSteps:  *steps,
		CreatedAt:       time.Now().UTC(),
	}
	if err := loops.ProcessPageFault(ctx, st, *repo, pf); err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{
			"ok":               true,
			"bound":            pf.ExtraReadSteps > 0,
			"extra_read_steps": pf.ExtraReadSteps,
			"resolved_symbols": pf.ResolvedSymbols,
		})
	}
	fmt.Fprintf(stdout, "bound page fault (steps=%d, symbols=%d)\n", pf.ExtraReadSteps, len(pf.ResolvedSymbols))
	return nil
}

func runReconcileDiff(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("reconcile-diff", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "Repository root directory")
	diffPath := fs.String("diff", "", "Path to unified git diff file ('-' for stdin; defaults to git diff HEAD~1..HEAD in --repo)")
	dbPath := fs.String("db", "", "Path to SQLite WAL memory.db")
	asJSON := fs.Bool("json", false, "Emit JSON output")

	if err := fs.Parse(hoistFlags(args, map[string]bool{"json": true})); err != nil {
		return err
	}

	var diffBytes []byte
	var err error
	dp := strings.TrimSpace(*diffPath)
	switch dp {
	case "-":
		diffBytes, err = io.ReadAll(os.Stdin)
		if err != nil {
			return fmt.Errorf("read diff from stdin: %w", err)
		}
	case "":
		cmd := exec.CommandContext(ctx, "git", "-C", *repo, "diff", "HEAD~1..HEAD")
		diffBytes, err = cmd.Output()
		if err != nil {
			// Fallback to working-tree diff when repo has <=1 commit.
			cmdWT := exec.CommandContext(ctx, "git", "-C", *repo, "diff", "HEAD")
			diffBytes, err = cmdWT.Output()
			if err != nil {
				return fmt.Errorf("read git diff in %q (or pass --diff <file>): %w", *repo, err)
			}
		}
	default:
		diffBytes, err = os.ReadFile(dp)
		if err != nil {
			return fmt.Errorf("read diff file %q: %w", dp, err)
		}
	}

	st, err := openStoreFromFlags(*dbPath)
	if err != nil {
		return err
	}
	defer st.Close()

	changes, err := loops.ReconcileASTRenames(ctx, st, *repo, string(diffBytes))
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(stdout).Encode(map[string]any{
			"ok":         true,
			"old_to_new": changes,
			"updated":    len(changes),
		})
	}
	fmt.Fprintf(stdout, "reconciled diff symbols: %d\n", len(changes))
	return nil
}
