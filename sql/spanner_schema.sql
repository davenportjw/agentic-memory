-- Google Cloud Spanner DDL Schema for User Profile Preferences & Session Summaries (Vector Search Enabled)

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
) PRIMARY KEY (UserId, PreferenceKey);

CREATE TABLE UserSessionSummaries (
    UserId STRING(MAX) NOT NULL,
    SessionId STRING(128) NOT NULL,
    SummaryText STRING(MAX),
    CreatedAt TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (UserId, SessionId);

CREATE TABLE ScopedMemories (
    ScopeType STRING(32) NOT NULL,
    ScopeId STRING(128) NOT NULL,
    MemoryKey STRING(128) NOT NULL,
    MemoryValue STRING(MAX) NOT NULL,
    MemoryType STRING(32),
    Category STRING(64),
    Confidence FLOAT64,
    CrystallizedBelief STRING(MAX),
    Keywords STRING(MAX),
    SourceSessionId STRING(MAX),
    PromotedFromScope STRING(128),
    SupportCount INT64,
    Embedding ARRAY<FLOAT64>,
    LastUpdated TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (ScopeType, ScopeId, MemoryKey);

-- Secondary index for fast category search
CREATE INDEX UserPreferencesByCategory
ON UserPreferences(UserId, Category);

CREATE INDEX ScopedMemoriesByTypeAndCategory
ON ScopedMemories(ScopeType, ScopeId, MemoryType, Category);

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
) PRIMARY KEY (mutation_id);

-- Note: In Spanner ENTERPRISE Edition, you can specify ARRAY<FLOAT64>(VECTOR_LENGTH=>768)
-- and create a VECTOR INDEX for approximate nearest neighbor (ANN) similarity search:
-- CREATE VECTOR INDEX UserPreferencesByEmbedding ON UserPreferences(Embedding) OPTIONS (distance_type='COSINE');

-- ============================================================================
-- EXAMPLE INPUT AND OUTPUT VECTOR QUERIES (COSINE_DISTANCE SIMILARITY SEARCH)
-- ============================================================================
--
-- Example Input Query (Upsert User Preference with 768-dimensional Vector Embedding):
-- INSERT OR UPDATE UserPreferences (
--     UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId,
--     Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated
-- ) VALUES (
--     @user_id, @pref_key, @pref_value, @category, @source_session_id,
--     @confidence, @crystallized_belief, @keywords, @embedding, PENDING_COMMIT_TIMESTAMP()
-- );
--
-- Example Output Query (Semantic Vector Similarity Lookup via COSINE_DISTANCE):
-- SELECT
--     UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId,
--     Confidence, CrystallizedBelief, Keywords, LastUpdated,
--     COSINE_DISTANCE(Embedding, @query_vector) AS distance
-- FROM UserPreferences
-- WHERE UserId = @user_id AND Embedding IS NOT NULL
-- ORDER BY COSINE_DISTANCE(Embedding, @query_vector) ASC
-- LIMIT 10;

