# Google Cloud Spanner Instance for Agent Profile Memory
resource "google_spanner_instance" "adk_instance" {
  name             = var.spanner_instance_id
  config           = "regional-${var.region}"
  display_name     = "ADK Agent Spanner Instance"
  processing_units = 100 # matches live; num_nodes = 1 would be 1000 PU (10x cost)
  project          = var.project_id

  depends_on = [google_project_service.required_apis]
}

# Google Cloud Spanner Database and Schema DDL
resource "google_spanner_database" "adk_database" {
  instance            = google_spanner_instance.adk_instance.name
  name                = var.spanner_database_id
  project             = var.project_id
  deletion_protection = false

  ddl = [
    <<EOD
CREATE TABLE UserPreferences (
  UserId STRING(MAX) NOT NULL,
  PreferenceKey STRING(128) NOT NULL,
  PreferenceValue STRING(MAX) NOT NULL,
  Category STRING(64),
  SourceSessionId STRING(MAX),
  Confidence FLOAT64,
  CrystallizedBelief STRING(MAX),
  Keywords STRING(MAX),
  Embedding ARRAY<FLOAT64>,
  LastUpdated TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (UserId, PreferenceKey)
EOD
    ,
    <<EOD
CREATE TABLE UserSessionSummaries (
  UserId STRING(MAX) NOT NULL,
  SessionId STRING(128) NOT NULL,
  SummaryText STRING(MAX),
  CreatedAt TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (UserId, SessionId)
EOD
    ,
    # Keep the original statements first and in order: Spanner DDL can only be appended in place;
    # reordering or editing earlier statements forces the database (and its data) to be recreated.
    "CREATE INDEX UserPreferencesByCategory ON UserPreferences(UserId, Category)",
    <<EOD
CREATE TABLE ScopedMemories (
  ScopeType STRING(32) NOT NULL,
  ScopeId STRING(128) NOT NULL,
  MemoryKey STRING(128) NOT NULL,
  MemoryValue STRING(MAX) NOT NULL,
  MemoryType STRING(32),
  Category STRING(64),
  FilePath STRING(512),
  Confidence FLOAT64,
  CrystallizedBelief STRING(MAX),
  Keywords STRING(MAX),
  SourceSessionId STRING(MAX),
  PromotedFromScope STRING(128),
  SupportCount INT64,
  HelpfulCount INT64,
  UnhelpfulCount INT64,
  WriteMode STRING(64),
  Embedding ARRAY<FLOAT64>,
  LastUpdated TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  UpdatedAt TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (ScopeType, ScopeId, MemoryKey)
EOD
    ,
    <<EOD
CREATE TABLE scoped_memories (
  memory_id STRING(128) NOT NULL,
  scope_type STRING(32) NOT NULL,
  scope_id STRING(128) NOT NULL,
  category STRING(64),
  file_path STRING(512) NOT NULL,
  memory_key STRING(128) NOT NULL,
  memory_value STRING(MAX) NOT NULL,
  crystallized_belief STRING(MAX),
  confidence FLOAT64,
  embedding ARRAY<FLOAT64>,
  helpful_count INT64,
  unhelpful_count INT64,
  write_mode STRING(64),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (memory_id)
EOD
    ,
    <<EOD
CREATE TABLE staged_memory_commits (
  commit_id STRING(128) NOT NULL,
  file_path STRING(512) NOT NULL,
  action STRING(32) NOT NULL,
  scope STRING(32) NOT NULL,
  scope_id STRING(128) NOT NULL,
  category STRING(64),
  proposed_content STRING(MAX),
  previous_content STRING(MAX),
  author STRING(256),
  summary STRING(1024),
  status STRING(32) NOT NULL,
  created_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (commit_id)
EOD
    ,
    <<EOD
CREATE TABLE scope_entities (
  entity_id STRING(128) NOT NULL,
  scope_type STRING(32) NOT NULL,
  name STRING(256) NOT NULL,
  path STRING(512) NOT NULL,
  identity_binding STRING(512),
  parent_path STRING(512),
  precedence_rank INT64,
  write_mode STRING(64),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (entity_id)
EOD
    ,
    <<EOD
CREATE TABLE memory_categories (
  category_id STRING(128) NOT NULL,
  name STRING(256) NOT NULL,
  folder_path STRING(256) NOT NULL,
  discovery_mode STRING(64),
  recall_trigger STRING(256),
  retention_ttl STRING(128),
  description STRING(MAX),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (category_id)
EOD
    ,
    <<EOD
CREATE TABLE ingest_services (
  service_id STRING(128) NOT NULL,
  name STRING(256) NOT NULL,
  stream_key STRING(128) NOT NULL,
  protocol STRING(128),
  status STRING(64),
  endpoint_url STRING(512),
  throughput_eps FLOAT64,
  avg_latency_ms FLOAT64,
  queue_depth INT64,
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (service_id)
EOD
    ,
    <<EOD
CREATE TABLE extractor_jobs (
  extractor_id STRING(128) NOT NULL,
  name STRING(256) NOT NULL,
  model_id STRING(128) NOT NULL,
  source_stream STRING(128),
  target_category STRING(128),
  write_mode STRING(64),
  human_params_json STRING(MAX),
  system_prompt STRING(MAX),
  status STRING(64),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (extractor_id)
EOD
    ,
    <<EOD
CREATE TABLE memory_audit_ledger (
  mutation_id STRING(128) NOT NULL,
  timestamp TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  actor STRING(256) NOT NULL,
  tool_name STRING(128),
  file_path STRING(512),
  scope STRING(64),
  write_mode STRING(64),
  prior_belief_snapshot STRING(MAX),
  new_belief_snapshot STRING(MAX),
  status STRING(64) NOT NULL
) PRIMARY KEY (mutation_id)
EOD
    ,
    <<EOD
CREATE TABLE rbac_policies (
  policy_id STRING(128) NOT NULL,
  principal STRING(512) NOT NULL,
  role_binding STRING(256),
  scope_path_pattern STRING(512) NOT NULL,
  allowed_actions_json STRING(MAX),
  write_governance STRING(128),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (policy_id)
EOD
    ,
    "CREATE INDEX ScopedMemoriesByTypeAndCategory ON ScopedMemories(ScopeType, ScopeId, MemoryType, Category)"
  ]
}
