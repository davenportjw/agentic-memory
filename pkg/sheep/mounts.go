package sheep

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// Mount describes a hierarchical /mem/* mount registered in sheep.
type Mount struct {
	Prefix      string `json:"prefix"`
	Backend     string `json:"backend"`
	ScopeType   string `json:"scope_type"`
	ReadOnly    bool   `json:"read_only"`
	SyncEnabled bool   `json:"sync_enabled"`
	Endpoint    string `json:"endpoint,omitempty"`
	Description string `json:"description"`
}

// SpannerConfig configures optional Cloud Spanner federated sync for /mem/project and /mem/org
// matching ScopedMemories in sql/spanner_schema.sql via Application Default Credentials (ADC).
type SpannerConfig struct {
	ProjectID   string
	InstanceID  string
	DatabaseID  string
	BaseURL     string
	HTTPClient  *http.Client
	TokenSource func(ctx context.Context) (string, error)
}

// DarkDataMountConfig configures an optional read-only HTTP dark-data memory provider.
type DarkDataMountConfig struct {
	Prefix   string
	Endpoint string
	Headers  map[string]string
	Timeout  time.Duration
}

// ScopedMemoryRow mirrors the Cloud Spanner ScopedMemories DDL table in sql/spanner_schema.sql.
type ScopedMemoryRow struct {
	ScopeType          string  `json:"ScopeType"`
	ScopeId            string  `json:"ScopeId"`
	MemoryKey          string  `json:"MemoryKey"`
	MemoryValue        string  `json:"MemoryValue"`
	MemoryType         string  `json:"MemoryType"`
	Category           string  `json:"Category"`
	Confidence         float64 `json:"Confidence"`
	CrystallizedBelief string  `json:"CrystallizedBelief"`
	Keywords           string  `json:"Keywords"`
	SourceSessionId    string  `json:"SourceSessionId"`
	PromotedFromScope  string  `json:"PromotedFromScope"`
	SupportCount       int64   `json:"SupportCount"`
}

// ListMounts returns the canonical default /mem/* hierarchy descriptors.
func ListMounts() []Mount {
	return defaultMounts(nil, nil)
}

// ListMounts returns the store's active /mem/* mounts including Spanner sync state and HTTP dark-data mounts.
func (s *Store) ListMounts() []Mount {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return defaultMounts(s.spanner, s.darkMounts)
}

func defaultMounts(sp *SpannerConfig, dark []DarkDataMountConfig) []Mount {
	spannerEnabled := sp != nil && sp.InstanceID != "" && sp.DatabaseID != ""
	projectBackend := "sqlite_repo"
	var spannerEndpoint string
	if spannerEnabled {
		projectBackend = "sqlite_spanner_federated"
		proj := sp.ProjectID
		if proj == "" {
			proj = defaultGCPProject()
		}
		spannerEndpoint = fmt.Sprintf("projects/%s/instances/%s/databases/%s", proj, sp.InstanceID, sp.DatabaseID)
	}

	mounts := []Mount{
		{
			Prefix:      "/mem/personal",
			Backend:     "sqlite_local",
			ScopeType:   "personal",
			ReadOnly:    false,
			SyncEnabled: false,
			Description: "Local-only developer preferences and personal beliefs (~/.toolroll/memory.db); never synced off-device.",
		},
		{
			Prefix:      "/mem/project",
			Backend:     projectBackend,
			ScopeType:   "project",
			ReadOnly:    false,
			SyncEnabled: spannerEnabled,
			Endpoint:    spannerEndpoint,
			Description: "Repo-scoped crystallized beliefs and symbol notes with optional Cloud Spanner ScopedMemories sync.",
		},
		{
			Prefix:      "/mem/org",
			Backend:     "org_policy",
			ScopeType:   "org",
			ReadOnly:    true,
			SyncEnabled: spannerEnabled,
			Endpoint:    spannerEndpoint,
			Description: "Read-only organization security and engineering policy beliefs.",
		},
	}

	for _, dm := range dark {
		mounts = append(mounts, Mount{
			Prefix:      dm.Prefix,
			Backend:     "http_dark_data",
			ScopeType:   "external",
			ReadOnly:    true,
			SyncEnabled: true,
			Endpoint:    dm.Endpoint,
			Description: "External HTTP dark-data memory mount provider.",
		})
	}
	return mounts
}

// RegisterDarkDataMount attaches a read-only external HTTP dark-data provider under /mem/*.
func (s *Store) RegisterDarkDataMount(cfg DarkDataMountConfig) error {
	prefix := strings.TrimSpace(cfg.Prefix)
	endpoint := strings.TrimSpace(cfg.Endpoint)
	if !strings.HasPrefix(prefix, "/mem/") || prefix == "/mem/" {
		return errors.New("sheep: dark-data mount prefix must start with /mem/")
	}
	if endpoint == "" {
		return errors.New("sheep: dark-data mount endpoint is required")
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 1500 * time.Millisecond
	}
	cfg.Prefix = prefix
	cfg.Endpoint = endpoint

	s.mu.Lock()
	defer s.mu.Unlock()
	for i, existing := range s.darkMounts {
		if existing.Prefix == prefix {
			s.darkMounts[i] = cfg
			return nil
		}
	}
	s.darkMounts = append(s.darkMounts, cfg)
	return nil
}

func (s *Store) queryDarkDataMounts(ctx context.Context, normRepo, intent string) ([]recallCandidate, error) {
	s.mu.RLock()
	mounts := append([]DarkDataMountConfig(nil), s.darkMounts...)
	s.mu.RUnlock()
	if len(mounts) == 0 {
		return nil, nil
	}

	var out []recallCandidate
	nowNs := time.Now().UTC().UnixNano()
	for _, dm := range mounts {
		notes, err := fetchDarkDataMount(ctx, dm, normRepo, intent)
		if err != nil {
			return nil, err
		}
		for _, n := range notes {
			content := strings.TrimSpace(n.Content)
			if content == "" {
				continue
			}
			conf := n.Confidence
			if conf <= 0 {
				conf = 0.75
			}
			out = append(out, recallCandidate{
				prefix:       dm.Prefix,
				body:         content,
				tokens:       extractLexicalTokens(content, nil),
				confidence:   conf,
				supportCount: 1,
				crystallized: true,
				createdAtNs:  nowNs,
			})
		}
	}
	return out, nil
}

type darkDataNote struct {
	Key        string  `json:"key"`
	Content    string  `json:"content"`
	Confidence float64 `json:"confidence"`
}

func fetchDarkDataMount(ctx context.Context, dm DarkDataMountConfig, repoRoot, intent string) ([]darkDataNote, error) {
	u, err := url.Parse(dm.Endpoint)
	if err != nil {
		return nil, err
	}
	q := u.Query()
	q.Set("repo", repoRoot)
	q.Set("intent", intent)
	u.RawQuery = q.Encode()

	reqCtx, cancel := context.WithTimeout(ctx, dm.Timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	for k, v := range dm.Headers {
		req.Header.Set(k, v)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("sheep: dark-data mount %s returned HTTP %d", dm.Prefix, resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return nil, err
	}

	var wrapped struct {
		Notes []darkDataNote `json:"notes"`
	}
	if err := json.Unmarshal(body, &wrapped); err == nil && wrapped.Notes != nil {
		return wrapped.Notes, nil
	}
	var direct []darkDataNote
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, err
	}
	return direct, nil
}

func defaultGCPProject() string {
	if p := strings.TrimSpace(os.Getenv("GOOGLE_CLOUD_PROJECT")); p != "" {
		return p
	}
	return "davenport-boutique"
}

func defaultADCToken(ctx context.Context) (string, error) {
	// 1. Check Cloud Run / GCE metadata server first for fast zero-subprocess token lookup.
	metaCtx, cancel := context.WithTimeout(ctx, 400*time.Millisecond)
	defer cancel()
	req, err := http.NewRequestWithContext(metaCtx, http.MethodGet, "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token", nil)
	if err == nil {
		req.Header.Set("Metadata-Flavor", "Google")
		if resp, err := http.DefaultClient.Do(req); err == nil {
			defer resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				var tok struct {
					AccessToken string `json:"access_token"`
				}
				if json.NewDecoder(resp.Body).Decode(&tok) == nil && tok.AccessToken != "" {
					return tok.AccessToken, nil
				}
			}
		}
	}

	// 2. Fall back to local gcloud Application Default Credentials.
	cmd := exec.CommandContext(ctx, "gcloud", "auth", "application-default", "print-access-token")
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("sheep: obtain Google Cloud ADC token: %w", err)
	}
	tok := strings.TrimSpace(string(out))
	if tok == "" {
		return "", errors.New("sheep: empty Google Cloud ADC token")
	}
	return tok, nil
}

// SyncSpannerProject synchronizes local /mem/project crystallized beliefs with Cloud Spanner ScopedMemories
// (sql/spanner_schema.sql) and pulls federated /mem/project + /mem/org beliefs into local SQLite WAL.
func (s *Store) SyncSpannerProject(ctx context.Context, repoRoot string) (pushed int, pulled int, err error) {
	if s.spanner == nil || s.spanner.InstanceID == "" || s.spanner.DatabaseID == "" {
		return 0, 0, errors.New("sheep: Cloud Spanner federated sync is not configured")
	}

	sp := s.spanner
	projectID := strings.TrimSpace(sp.ProjectID)
	if projectID == "" {
		projectID = defaultGCPProject()
	}
	baseURL := strings.TrimRight(strings.TrimSpace(sp.BaseURL), "/")
	if baseURL == "" {
		baseURL = "https://spanner.googleapis.com"
	}
	httpClient := sp.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	tokFn := sp.TokenSource
	if tokFn == nil {
		tokFn = defaultADCToken
	}
	token, err := tokFn(ctx)
	if err != nil {
		return 0, 0, err
	}

	normRepo := normalizeRepoRoot(repoRoot)
	dbResource := fmt.Sprintf("projects/%s/instances/%s/databases/%s", projectID, sp.InstanceID, sp.DatabaseID)

	// 1. Create Spanner session.
	var sessResp struct {
		Name string `json:"name"`
	}
	if err := spannerPostJSON(ctx, httpClient, token, baseURL+"/v1/"+dbResource+"/sessions", map[string]any{}, &sessResp); err != nil {
		return 0, 0, fmt.Errorf("sheep: create spanner session: %w", err)
	}
	if sessResp.Name == "" {
		return 0, 0, errors.New("sheep: spanner returned empty session name")
	}

	// 2. Push unsynced or updated local /mem/project crystallized beliefs via ScopedMemories InsertOrUpdate.
	rows, err := s.db.QueryContext(ctx, `
		SELECT scope_type, scope_id, memory_key, memory_value, memory_type, category,
		       confidence, crystallized_belief, keywords, source_session_id, promoted_from_scope, support_count
		FROM scoped_memories
		WHERE scope_type = 'project' AND scope_id = ? AND last_updated_ns > synced_at_ns`,
		normRepo,
	)
	if err != nil {
		return 0, 0, fmt.Errorf("sheep: query unsynced project beliefs: %w", err)
	}
	var values [][]any
	var syncedKeys []string
	for rows.Next() {
		var r ScopedMemoryRow
		if err := rows.Scan(
			&r.ScopeType, &r.ScopeId, &r.MemoryKey, &r.MemoryValue, &r.MemoryType, &r.Category,
			&r.Confidence, &r.CrystallizedBelief, &r.Keywords, &r.SourceSessionId, &r.PromotedFromScope, &r.SupportCount,
		); err != nil {
			_ = rows.Close()
			return 0, 0, err
		}
		values = append(values, []any{
			r.ScopeType, r.ScopeId, r.MemoryKey, r.MemoryValue, r.MemoryType, r.Category,
			r.Confidence, r.CrystallizedBelief, r.Keywords, r.SourceSessionId, r.PromotedFromScope,
			strconv.FormatInt(r.SupportCount, 10), "spanner.commit_timestamp()",
		})
		syncedKeys = append(syncedKeys, r.MemoryKey)
	}
	_ = rows.Close()

	nowNs := time.Now().UTC().UnixNano()
	if len(values) > 0 {
		commitBody := map[string]any{
			"singleUseTransaction": map[string]any{"readWrite": map[string]any{}},
			"mutations": []map[string]any{
				{
					"insertOrUpdate": map[string]any{
						"table": "ScopedMemories",
						"columns": []string{
							"ScopeType", "ScopeId", "MemoryKey", "MemoryValue", "MemoryType", "Category",
							"Confidence", "CrystallizedBelief", "Keywords", "SourceSessionId",
							"PromotedFromScope", "SupportCount", "LastUpdated",
						},
						"values": values,
					},
				},
			},
		}
		if err := spannerPostJSON(ctx, httpClient, token, baseURL+"/v1/"+sessResp.Name+":commit", commitBody, nil); err != nil {
			return 0, 0, fmt.Errorf("sheep: commit ScopedMemories to spanner: %w", err)
		}
		for _, k := range syncedKeys {
			_, _ = s.db.ExecContext(ctx, `
				UPDATE scoped_memories SET synced_at_ns = ?
				WHERE scope_type = 'project' AND scope_id = ? AND memory_key = ?`,
				nowNs, normRepo, k,
			)
		}
		pushed = len(values)
	}

	// 3. Pull federated /mem/project + /mem/org rows from Spanner ScopedMemories.
	sqlReq := map[string]any{
		"sql": `SELECT ScopeType, ScopeId, MemoryKey, MemoryValue, MemoryType, Category,
		               Confidence, CrystallizedBelief, Keywords, SourceSessionId, PromotedFromScope, SupportCount
		        FROM ScopedMemories
		        WHERE (ScopeType = 'project' AND ScopeId = @scope_id) OR ScopeType = 'org'`,
		"params": map[string]any{
			"scope_id": normRepo,
		},
		"paramTypes": map[string]any{
			"scope_id": map[string]any{"code": "STRING"},
		},
	}
	var sqlResp struct {
		Rows [][]any `json:"rows"`
	}
	if err := spannerPostJSON(ctx, httpClient, token, baseURL+"/v1/"+sessResp.Name+":executeSql", sqlReq, &sqlResp); err != nil {
		return pushed, 0, fmt.Errorf("sheep: executeSql ScopedMemories: %w", err)
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return pushed, 0, err
	}
	defer func() { _ = tx.Rollback() }()

	for _, rawRow := range sqlResp.Rows {
		if len(rawRow) < 12 {
			continue
		}
		r := ScopedMemoryRow{
			ScopeType:          asString(rawRow[0]),
			ScopeId:            asString(rawRow[1]),
			MemoryKey:          asString(rawRow[2]),
			MemoryValue:        asString(rawRow[3]),
			MemoryType:         asString(rawRow[4]),
			Category:           asString(rawRow[5]),
			Confidence:         asFloat(rawRow[6]),
			CrystallizedBelief: asString(rawRow[7]),
			Keywords:           asString(rawRow[8]),
			SourceSessionId:    asString(rawRow[9]),
			PromotedFromScope:  asString(rawRow[10]),
			SupportCount:       asInt64(rawRow[11]),
		}
		if r.ScopeType == "" || r.MemoryKey == "" || r.CrystallizedBelief == "" {
			continue
		}
		if r.ScopeId == "" {
			r.ScopeId = "global"
		}
		tokens := extractLexicalTokens(r.CrystallizedBelief+" "+r.Keywords, nil)
		keywords := strings.Join(tokens, " ")

		if _, err := tx.ExecContext(ctx, `
			INSERT INTO scoped_memories (
				scope_type, scope_id, memory_key, memory_value, memory_type, category,
				confidence, crystallized_belief, keywords, symbols_json,
				source_session_id, promoted_from_scope, support_count, synced_at_ns, last_updated_ns
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
			ON CONFLICT(scope_type, scope_id, memory_key) DO UPDATE SET
				memory_value = excluded.memory_value,
				memory_type = excluded.memory_type,
				category = excluded.category,
				confidence = excluded.confidence,
				crystallized_belief = excluded.crystallized_belief,
				keywords = excluded.keywords,
				support_count = excluded.support_count,
				synced_at_ns = excluded.synced_at_ns,
				last_updated_ns = excluded.last_updated_ns`,
			r.ScopeType, r.ScopeId, r.MemoryKey, r.MemoryValue, r.MemoryType, r.Category,
			r.Confidence, r.CrystallizedBelief, keywords,
			r.SourceSessionId, r.PromotedFromScope, r.SupportCount, nowNs, nowNs,
		); err != nil {
			return pushed, 0, err
		}
		repoForTokens := r.ScopeId
		docKey := fmt.Sprintf("mem:%s|%s|%s", r.ScopeType, r.ScopeId, r.MemoryKey)
		if err := indexTokensTx(ctx, tx, s.ftsEnabled, "mem", docKey, repoForTokens, "/mem/"+r.ScopeType, r.CrystallizedBelief, tokens); err != nil {
			return pushed, 0, err
		}
		pulled++
	}

	if err := tx.Commit(); err != nil {
		return pushed, 0, err
	}
	return pushed, pulled, nil
}

func spannerPostJSON(ctx context.Context, client *http.Client, token, endpoint string, reqBody any, out any) error {
	payload, err := json.Marshal(reqBody)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2*1024*1024))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if out != nil && len(body) > 0 {
		if err := json.Unmarshal(body, out); err != nil {
			return err
		}
	}
	return nil
}

func asString(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

func asFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case string:
		f, _ := strconv.ParseFloat(x, 64)
		return f
	default:
		return 0
	}
}

func asInt64(v any) int64 {
	switch x := v.(type) {
	case float64:
		return int64(x)
	case int64:
		return x
	case string:
		n, _ := strconv.ParseInt(x, 10, 64)
		return n
	default:
		return 0
	}
}
