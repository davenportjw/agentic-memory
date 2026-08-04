-- BigQuery SQL Pipeline using AI.GENERATE / ML.GENERATE_TEXT to Summarize Sessions
-- This query processes the raw telemetry logs in BigQuery, aggregates user prompts and agent responses,
-- and invokes Gemini via Vertex AI BigQuery Remote Models to summarize user profile learnings.
--
-- Note: Requires a BigQuery Remote Model (configured in Terraform or created via DDL below).

-- Example DDL to create the Remote Model:
-- CREATE OR REPLACE MODEL `${PROJECT_ID}.${BQ_DATASET_ID}.gemini_summarizer`
-- REMOTE WITH CONNECTION `${PROJECT_ID}.us.vertex_ai_conn`
-- OPTIONS (ENDPOINT = 'gemini-3.5-flash-lite');

-- 1. Aggregation & Summarization Query using BigQuery AI / ML.GENERATE_TEXT
WITH SessionTranscripts AS (
  SELECT
    user_id,
    session_id,
    STRING_AGG(
      CONCAT(event_type, ' [', FORMAT_TIMESTAMP('%T', timestamp), ']: ', message),
      '\n' ORDER BY timestamp ASC
    ) AS full_transcript,
    MAX(timestamp) AS last_interaction
  FROM `${PROJECT_ID}.${BQ_DATASET_ID}.agent_logs`
  WHERE agent_name = 'InterviewAgent'
  GROUP BY user_id, session_id
),
PromptedSessions AS (
  SELECT
    user_id,
    session_id,
    last_interaction,
    CONCAT(
      'You are an expert AI Profiler. Analyze the following conversation transcript between a user (user_id=', user_id, ') ',
      'and the InterviewAgent.\n',
      'Extract all key user preferences, technical skills, job role, favorite tools, languages, and work style.\n',
      'Return a valid JSON object matching this schema:\n',
      '{\n',
      '  "user_id": "', user_id, '",\n',
      '  "session_id": "', session_id, '",\n',
      '  "summary": "Concise 2-sentence summary of the user profile",\n',
      '  "preferences": [\n',
      '    {"preference_key": "favorite_language", "preference_value": "Python", "category": "technical", "confidence": 0.95},\n',
      '    {"preference_key": "job_role", "preference_value": "Software Engineer", "category": "role", "confidence": 1.0}\n',
      '  ]\n',
      '}\n\n',
      'Transcript:\n', full_transcript
    ) AS prompt
  FROM SessionTranscripts
)
SELECT
  user_id,
  session_id,
  last_interaction,
  ml_generate_text_result['candidates'][0]['content']['parts'][0]['text'] AS generated_summary_json
FROM
  ML.GENERATE_TEXT(
    MODEL `${PROJECT_ID}.${BQ_DATASET_ID}.gemini_summarizer`,
    (SELECT user_id, session_id, last_interaction, prompt FROM PromptedSessions),
    STRUCT(
      0.2 AS temperature,
      1024 AS max_output_tokens,
      TRUE AS flatten_json_output
    )
  );

-- 2. Alternative SQL using BigQuery AI.GENERATE syntax (if using newer BigQuery AI functions)
-- SELECT
--   user_id,
--   session_id,
--   AI.GENERATE(
--     (SELECT full_transcript FROM SessionTranscripts WHERE SessionTranscripts.session_id = t.session_id),
--     'Extract user profile preferences and return as JSON with keys: favorite_language, job_role, preferred_tools'
--   ) AS ai_summary
-- FROM SessionTranscripts t;
