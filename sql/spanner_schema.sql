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

-- Secondary index for fast category search
CREATE INDEX UserPreferencesByCategory
ON UserPreferences(UserId, Category);

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

