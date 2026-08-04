# Google Cloud Spanner Instance for Agent Profile Memory
resource "google_spanner_instance" "adk_instance" {
  name         = var.spanner_instance_id
  config       = "regional-${var.region}"
  display_name = "ADK Agent Memory"
  num_nodes    = 1
  project      = var.project_id

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
    "CREATE INDEX UserPreferencesByCategory ON UserPreferences(UserId, Category)"
  ]
}
