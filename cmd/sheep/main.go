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
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/davenportjw/electric-sheep/pkg/sheep"
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
  sheep recall  --repo <dir> --intent "..." [--mount scoped|full|clean] [--db <path>] [--json]
  sheep observe --repo <dir> --session <id> --scope /mem/project [--symbols SymA,SymB] [--db <path>] "..."
  sheep note    --repo <dir> [--db <path>] <Symbol>
  sheep dream   --repo <dir> [--db <path>] [--json]
  sheep evict   --repo <dir> [--db <path>] OldSym=NewSym [DeletedSym= ...]
  sheep mounts  [--db <path>] [--json]
  sheep serve   [--addr 127.0.0.1:7410] [--db <path>]
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
