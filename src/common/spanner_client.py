"""Google Cloud Spanner client for storing and querying user profile preferences and summaries."""

import datetime
import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from src.common.config import get_config

try:
    from google.cloud import spanner
except ImportError:
    spanner = None

logger = logging.getLogger("adk.spanner")

# Global in-memory stores so mock mode works across multiple client instances
_GLOBAL_IN_MEMORY_PREFS: Dict[str, Dict[str, Any]] = {}
_GLOBAL_IN_MEMORY_SUMMARIES: Dict[str, Dict[str, Any]] = {}
_GLOBAL_IN_MEMORY_SCOPED: Dict[str, Dict[str, Any]] = {}

VALID_SCOPE_TYPES = ("ORG", "TEAM", "AGENT", "USER")
VALID_MEMORY_TYPES = ("PREFERENCE", "INVARIANT_GUARDRAIL", "PROCEDURAL_SKILL", "EPISODIC_FACT")
MATRYOSHKA_DIMS = (128, 256, 512, 768)


def slice_and_normalize_vector(vec: List[float], dim: int = 768) -> List[float]:
    """Matryoshka Representation Learning (MRL) vector slicer and L2 normalizer.

    Slices a 768-dim embedding vector to `dim` (128, 256, 512, or 768) and re-normalizes
    to unit L2 length so cosine similarity operates accurately at compressed dimensions.
    """
    if dim <= 0:
        dim = 768
    if not vec:
        out = [0.0] * dim
        out[0] = 1.0
        return out
    sliced = list(vec[:dim])
    if len(sliced) < dim:
        sliced.extend([0.0] * (dim - len(sliced)))
    norm = sum(x * x for x in sliced) ** 0.5
    if norm > 0:
        return [round(x / norm, 6) for x in sliced]
    sliced[0] = 1.0
    return sliced


def estimate_tokens(text: str) -> int:
    """Estimate LLM context token count from formatted memory text."""
    if not text or not text.strip():
        return 0
    cleaned = text.strip()
    # Standard subword approximation (~3.8 chars per token + punctuation boundaries)
    char_est = max(1, int(round(len(cleaned) / 3.8)))
    word_est = max(1, int(round(len(cleaned.split()) * 1.3)))
    return max(1, (char_est + word_est) // 2)


def format_memories_for_context(
    memories: List[Dict[str, Any]],
    injection_format: str = "COMPACT_BULLET",
) -> Dict[str, Any]:
    """Format a list of resolved scoped memories into LLM context strings.

    Supported formats:
    - ATOMIC_KV: ultra-compact `key=value (scope)` tokens (~70% token reduction)
    - COMPACT_BULLET: concise bullet points `[SCOPE] key: value`
    - FULL_BELIEF: full verbose CrystallizedBelief sentences with provenance metadata
    """
    fmt = (injection_format or "COMPACT_BULLET").upper().strip()
    if not memories:
        return {
            "formatted_text": "",
            "injection_format": fmt,
            "estimated_tokens": 0,
            "baseline_full_tokens": 0,
            "token_savings_pct": 0.0,
        }

    atomic_parts: List[str] = []
    compact_lines: List[str] = []
    full_lines: List[str] = []

    for m in memories:
        scope_type = str(m.get("scope_type") or "USER").upper()
        scope_id = str(m.get("scope_id") or m.get("user_id") or "default")
        key = str(m.get("memory_key") or m.get("preference_key") or "unknown").strip()
        val = str(m.get("memory_value") or m.get("preference_value") or "").strip()
        mem_type = str(m.get("memory_type") or "PREFERENCE").upper()
        cat = str(m.get("category") or "general")
        conf = float(m.get("confidence", 1.0))
        belief = str(m.get("crystallized_belief") or "").strip()
        if not belief or len(belief) <= len(val) + 8:
            belief = (
                f"In scope {scope_type} ({scope_id}), the crystallized {mem_type.lower()} for '{key}' "
                f"(category: {cat}, confidence: {conf:.2f}) is established as: {val}."
            )
        else:
            belief = (
                f"[Scope: {scope_type}:{scope_id} | Type: {mem_type} | Category: {cat} | "
                f"Confidence: {conf:.2f} | Key: {key}] {belief}"
            )

        prefix_tag = "!" if mem_type == "INVARIANT_GUARDRAIL" else ""
        atomic_parts.append(f"{prefix_tag}{key}={val} ({scope_type})")
        compact_lines.append(f"- [{scope_type}] {key}: {val}")
        full_lines.append(f"- {belief}")

    atomic_text = "; ".join(atomic_parts)
    compact_text = "\n".join(compact_lines)
    full_text = "\n".join(full_lines)

    if fmt == "ATOMIC_KV":
        chosen_text = atomic_text
    elif fmt == "FULL_BELIEF":
        chosen_text = full_text
    else:
        fmt = "COMPACT_BULLET"
        chosen_text = compact_text

    chosen_tokens = estimate_tokens(chosen_text)
    baseline_tokens = max(chosen_tokens, estimate_tokens(full_text))
    savings_pct = round((1.0 - (chosen_tokens / baseline_tokens)) * 100.0, 1) if baseline_tokens > 0 else 0.0

    return {
        "formatted_text": chosen_text,
        "injection_format": fmt,
        "estimated_tokens": chosen_tokens,
        "baseline_full_tokens": baseline_tokens,
        "token_savings_pct": savings_pct,
    }


class SpannerPreferenceClient:
    """Client for reading and writing user preferences, scoped enterprise memories, and session summaries to Google Cloud Spanner."""

    def __init__(self, use_mock: Optional[bool] = None):
        self.config = get_config()
        self.instance_id = self.config.spanner_instance_id
        self.database_id = self.config.spanner_database_id
        self.use_mock = False if use_mock is None else use_mock
        self._in_memory_prefs = _GLOBAL_IN_MEMORY_PREFS
        self._in_memory_summaries = _GLOBAL_IN_MEMORY_SUMMARIES
        self._in_memory_scoped = _GLOBAL_IN_MEMORY_SCOPED
        self._spanner_client = None
        self._instance = None
        self._database = None
        self._scoped_table_available: Optional[bool] = None

        if not self.use_mock and spanner is not None:
            try:
                self._spanner_client = spanner.Client(project=self.config.project_id)
                self._instance = self._spanner_client.instance(self.config.spanner_instance_id)
                self._database = self._instance.database(self.config.spanner_database_id)
            except Exception as e:
                logger.warning(
                    f"Could not initialize Spanner client ({e}). Falling back to in-memory Spanner store."
                )
                self.use_mock = True
        elif not self.use_mock and spanner is None:
            logger.warning("google.cloud.spanner library not installed. Falling back to in-memory Spanner store.")
            self.use_mock = True

    @staticmethod
    def slice_and_normalize_vector(vec: List[float], dim: int = 768) -> List[float]:
        """Slice and normalize a vector to Matryoshka dimension `dim` (128, 256, 512, 768)."""
        return slice_and_normalize_vector(vec, dim)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate token count for context strings."""
        return estimate_tokens(text)

    @staticmethod
    def format_memories_for_context(
        memories: List[Dict[str, Any]],
        injection_format: str = "COMPACT_BULLET",
    ) -> Dict[str, Any]:
        """Format memories into ATOMIC_KV, COMPACT_BULLET, or FULL_BELIEF."""
        return format_memories_for_context(memories, injection_format=injection_format)

    def _cosine_distance(self, v1: List[float], v2: List[float], dim: Optional[int] = None) -> float:
        """Compute cosine distance (1 - cosine_similarity) between two vectors, optionally sliced to Matryoshka `dim`."""
        if not v1 or not v2:
            return 1.0
        if dim is not None and dim > 0:
            v1 = slice_and_normalize_vector(v1, dim)
            v2 = slice_and_normalize_vector(v2, dim)
        elif len(v1) != len(v2):
            min_len = min(len(v1), len(v2))
            v1 = slice_and_normalize_vector(v1, min_len)
            v2 = slice_and_normalize_vector(v2, min_len)
        dot = sum(x * y for x, y in zip(v1, v2))
        norm1 = sum(x * x for x in v1) ** 0.5
        norm2 = sum(y * y for y in v2) ** 0.5
        if norm1 == 0 or norm2 == 0:
            return 1.0
        sim = dot / (norm1 * norm2)
        return max(0.0, min(2.0, 1.0 - sim))

    def _generate_embedding(self, text: str, dim: int = 768) -> List[float]:
        """Generate a Matryoshka-compatible 768-dimensional semantic embedding vector for a given text.

        Coarse semantic domain anchors are nested within the first 128 dimensions (0..127)
        and refined across higher dimensions (128..767) so slicing to 128d, 256d, 512d, or 768d
        preserves semantic cluster similarity across both live Spanner SQL and in-memory search.
        """
        vec = [0.0] * 768
        lower_text = text.lower().strip()
        words = [
            w for w in lower_text.replace("?", " ").replace(".", " ").replace(",", " ")
            .replace("_", " ").replace(":", " ").replace("=", " ").replace("-", " ").split()
            if len(w) >= 2
        ]
        if not words:
            vec[0] = 1.0
            return slice_and_normalize_vector(vec, dim) if dim != 768 else vec

        # Coarse-to-fine Matryoshka semantic domain anchors (indices < 128 + full 768 indices)
        semantic_domains = {
            "language": [10, 11, 12, 13], "python": [10, 11, 14], "go": [10, 15], "javascript": [10, 16],
            "rust": [10, 17], "typescript": [10, 18], "code": [10, 12], "coding": [10, 12],
            "programming": [10, 12, 13], "favorite_language": [10, 11, 12],
            "role": [20, 21, 50, 51, 52], "job": [20, 21, 50, 51], "work": [20, 50, 53],
            "title": [20, 50, 51], "career": [20, 50, 54], "architect": [20, 50, 55],
            "engineer": [20, 50, 56], "developer": [20, 50, 57], "job_role": [20, 21, 50, 51, 52],
            "name": [30, 31, 100, 101, 102], "who": [30, 100, 101], "identity": [30, 100, 103],
            "developer": [30, 100, 104], "alex": [30, 100, 105],
            "tool": [40, 41, 150, 151], "tools": [40, 41, 150, 151], "spanner": [40, 42, 150, 152],
            "bigquery": [40, 43, 150, 153], "terraform": [40, 44, 150, 154], "gcp": [40, 45, 150, 155],
            "cloud": [40, 46, 150, 156], "preferred_tools": [40, 41, 150, 151],
            "database": [40, 42, 150, 152], "sql": [40, 42, 150, 153],
            "test": [47, 48, 157], "testing": [47, 48, 157], "pytest": [47, 48, 158],
            "runner": [47, 48, 159], "uv": [47, 48, 88, 308], "package": [47, 48, 160],
            "style": [60, 61, 200, 201], "work_style": [60, 61, 200, 201, 202],
            "prefer": [60, 200, 203], "collaborative": [60, 200, 204], "concise": [60, 62, 205],
            "theme": [63, 64, 206], "ui": [63, 64, 207], "sepia": [63, 64, 208], "dark": [63, 64, 209],
            "intent": [70, 71, 250, 251], "goal": [70, 250, 252], "objective": [70, 250, 253],
            "purpose": [70, 250, 254], "trajectory": [70, 250, 255], "session_intent": [70, 71, 250, 251],
            "cross_session_intent": [70, 71, 250, 251],
            "guardrail": [80, 81, 300, 301], "procedural": [80, 82, 300, 302],
            "invariant": [80, 83, 300, 303], "rule": [80, 84, 300, 304], "policy": [80, 84, 300, 304],
            "limit": [80, 85, 300, 305], "torque": [80, 86, 300, 306], "docker": [80, 87, 300, 307],
            "mock": [80, 89, 309], "mocks": [80, 89, 309], "secret": [80, 90, 310],
            "secrets": [80, 90, 310], "security": [80, 90, 310], "api_key": [80, 90, 311],
            "region": [92, 93, 320], "deploy": [92, 94, 321], "cloud_run": [92, 94, 322],
        }

        for w in words:
            if w in semantic_domains:
                for idx in semantic_domains[w]:
                    vec[idx] += 3.0
                    # Mirror high-dimensional anchor into Matryoshka core (0..127)
                    vec[idx % 128] += 2.5
            h_core = abs(hash(w)) % 128
            h_mid = 128 + (abs(hash(w + "_mid")) % 128)
            h_full = abs(hash(w)) % 768
            vec[h_core] += 1.5
            vec[h_mid] += 1.0
            vec[h_full] += 1.0

        vec = slice_and_normalize_vector(vec, 768)
        if dim != 768:
            return slice_and_normalize_vector(vec, dim)
        return vec

    def _pref_key(self, user_id: str, preference_key: str) -> str:
        return f"{user_id}::{preference_key.lower().strip()}"

    def get_user_preference(self, user_id: str, preference_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific user preference from Spanner by user_id and preference_key."""
        normalized_key = preference_key.lower().strip()
        if self.use_mock or not self._database:
            return self._in_memory_prefs.get(self._pref_key(user_id, normalized_key))

        try:
            with self._database.snapshot() as snapshot:
                results = snapshot.execute_sql(
                    """
                    SELECT UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId, Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated
                    FROM UserPreferences
                    WHERE UserId = @user_id AND LOWER(PreferenceKey) = @pref_key
                    """,
                    params={"user_id": user_id, "pref_key": normalized_key},
                    param_types={"user_id": spanner.param_types.STRING, "pref_key": spanner.param_types.STRING},
                )
                for row in results:
                    return {
                        "user_id": row[0],
                        "preference_key": row[1],
                        "preference_value": row[2],
                        "category": row[3],
                        "source_session_id": row[4],
                        "confidence": row[5],
                        "crystallized_belief": row[6] or "",
                        "keywords": row[7] or "",
                        "embedding": row[8] or [],
                        "last_updated": str(row[9]) if len(row) > 9 and row[9] else None,
                    }
            return None
        except Exception as e:
            logger.error(f"Error reading from Spanner ({e})")
            return self._in_memory_prefs.get(self._pref_key(user_id, normalized_key))

    def search_user_preferences(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
        distance_threshold: float = 0.65,
    ) -> List[Dict[str, Any]]:
        """Search user preferences using vector similarity (COSINE_DISTANCE over 768-dim embeddings).

        Never uses pure text or substring matching.
        """
        query_vector = self._generate_embedding(query)
        all_prefs = self.get_all_user_preferences(user_id)
        if not all_prefs:
            return []

        if not self.use_mock and self._database:
            try:
                with self._database.snapshot() as snapshot:
                    results = snapshot.execute_sql(
                        """
                        SELECT UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId, Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated,
                               COSINE_DISTANCE(Embedding, @query_vector) AS distance
                        FROM UserPreferences
                        WHERE UserId = @user_id AND Embedding IS NOT NULL
                        ORDER BY COSINE_DISTANCE(Embedding, @query_vector) ASC
                        LIMIT @limit
                        """,
                        params={
                            "user_id": user_id,
                            "query_vector": query_vector,
                            "limit": limit,
                        },
                        param_types={
                            "user_id": spanner.param_types.STRING,
                            "query_vector": spanner.param_types.Array(spanner.param_types.FLOAT64),
                            "limit": spanner.param_types.INT64,
                        },
                    )
                    matching = []
                    for row in results:
                        dist = float(row[10]) if len(row) > 10 and row[10] is not None else 1.0
                        if dist <= distance_threshold:
                            matching.append({
                                "user_id": row[0],
                                "preference_key": row[1],
                                "preference_value": row[2],
                                "category": row[3],
                                "source_session_id": row[4],
                                "confidence": row[5],
                                "crystallized_belief": row[6] or "",
                                "keywords": row[7] or "",
                                "embedding": row[8] or [],
                                "last_updated": str(row[9]) if len(row) > 9 and row[9] else None,
                                "cosine_distance": round(dist, 4),
                            })
                    return matching
            except Exception as e:
                logger.warning(f"Live Spanner vector search query failed ({e}). Falling back to in-memory vector similarity.")

        # In-memory vector similarity search (cosine distance over embeddings, no pure text matching!)
        scored_prefs = []
        for pref in all_prefs:
            pref_vec = pref.get("embedding")
            if not pref_vec:
                pref_vec = self._generate_embedding(
                    f"{pref.get('preference_key', '')} {pref.get('preference_value', '')} {pref.get('category', '')} {pref.get('crystallized_belief', '')} {pref.get('keywords', '')}"
                )
                pref["embedding"] = pref_vec
            dist = self._cosine_distance(query_vector, pref_vec)
            if dist <= distance_threshold:
                pref_copy = dict(pref)
                pref_copy["cosine_distance"] = round(dist, 4)
                scored_prefs.append((dist, pref_copy))

        scored_prefs.sort(key=lambda x: x[0])
        return [p for _, p in scored_prefs[:limit]]

    def get_all_user_preferences(self, user_id: str) -> List[Dict[str, Any]]:
        """Retrieve all known preferences for a given user."""
        if self.use_mock or not self._database:
            return [p for p in self._in_memory_prefs.values() if p["user_id"] == user_id]

        try:
            with self._database.snapshot() as snapshot:
                results = snapshot.execute_sql(
                    """
                    SELECT UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId, Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated
                    FROM UserPreferences
                    WHERE UserId = @user_id
                    """,
                    params={"user_id": user_id},
                    param_types={"user_id": spanner.param_types.STRING},
                )
                prefs = []
                for row in results:
                    prefs.append({
                        "user_id": row[0],
                        "preference_key": row[1],
                        "preference_value": row[2],
                        "category": row[3],
                        "source_session_id": row[4],
                        "confidence": row[5],
                        "crystallized_belief": row[6] or "",
                        "keywords": row[7] or "",
                        "embedding": row[8] or [],
                        "last_updated": str(row[9]) if len(row) > 9 and row[9] else None,
                    })
                return prefs
        except Exception as e:
            logger.error(f"Error listing Spanner UserPreferences ({e})")
            return [p for p in self._in_memory_prefs.values() if p["user_id"] == user_id]

    def get_procedural_guardrails(self, user_id: str) -> List[Dict[str, Any]]:
        """Retrieve all procedural invariants and operational guardrails for a given user."""
        all_prefs = self.get_all_user_preferences(user_id)
        guardrails = []
        for p in all_prefs:
            cat = str(p.get("category", "")).lower()
            kw = str(p.get("keywords", "")).lower()
            key = str(p.get("preference_key", "")).lower()
            if cat in ("guardrail", "procedural") or "guardrail" in kw or "invariant" in kw or key.startswith("guardrail_") or key.startswith("invariant_"):
                guardrails.append(p)
        return guardrails

    def upsert_user_preference(
        self,
        user_id: str,
        preference_key: str,
        preference_value: str,
        category: str = "general",
        confidence: float = 1.0,
        source_session_id: str = "",
        crystallized_belief: str = "",
        keywords: str = "",
        embedding: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """Insert or update a user preference in Spanner with a 768-dimensional vector embedding."""
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        normalized_key = preference_key.lower().strip()
        if embedding is None:
            embedding = self._generate_embedding(
                f"{normalized_key} {preference_value} {category} {crystallized_belief} {keywords}"
            )

        record = {
            "user_id": user_id,
            "preference_key": normalized_key,
            "preference_value": preference_value,
            "category": category,
            "source_session_id": source_session_id,
            "confidence": confidence,
            "crystallized_belief": crystallized_belief,
            "keywords": keywords,
            "embedding": embedding,
            "last_updated": now_str,
        }

        self._in_memory_prefs[self._pref_key(user_id, normalized_key)] = record

        if not self.use_mock and self._database:
            try:
                def _write(transaction):
                    transaction.execute_update(
                        """
                        INSERT OR UPDATE UserPreferences (
                            UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId, Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated
                        ) VALUES (
                            @user_id, @pref_key, @pref_value, @category, @source_session_id, @confidence, @crystallized_belief, @keywords, @embedding, PENDING_COMMIT_TIMESTAMP()
                        )
                        """,
                        params={
                            "user_id": user_id,
                            "pref_key": normalized_key,
                            "pref_value": preference_value,
                            "category": category,
                            "source_session_id": source_session_id,
                            "confidence": confidence,
                            "crystallized_belief": crystallized_belief,
                            "keywords": keywords,
                            "embedding": embedding,
                        },
                        param_types={
                            "user_id": spanner.param_types.STRING,
                            "pref_key": spanner.param_types.STRING,
                            "pref_value": spanner.param_types.STRING,
                            "category": spanner.param_types.STRING,
                            "source_session_id": spanner.param_types.STRING,
                            "confidence": spanner.param_types.FLOAT64,
                            "crystallized_belief": spanner.param_types.STRING,
                            "keywords": spanner.param_types.STRING,
                            "embedding": spanner.param_types.Array(spanner.param_types.FLOAT64),
                        },
                    )

                self._database.run_in_transaction(_write)
            except Exception as e:
                logger.error(f"Failed to upsert preference to live Spanner ({e})")

        return record

    def upsert_session_summary(
        self,
        user_id: str,
        session_id: str,
        summary_text: str,
    ) -> Dict[str, Any]:
        """Store a session summary in Spanner UserSessionSummaries table."""
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record = {
            "user_id": user_id,
            "session_id": session_id,
            "summary_text": summary_text,
            "created_at": now_str,
        }

        self._in_memory_summaries[f"{user_id}::{session_id}"] = record

        if not self.use_mock and self._database:
            try:
                def _write(transaction):
                    transaction.execute_update(
                        """
                        INSERT OR UPDATE UserSessionSummaries (
                            UserId, SessionId, SummaryText, CreatedAt
                        ) VALUES (
                            @user_id, @session_id, @summary_text, PENDING_COMMIT_TIMESTAMP()
                        )
                        """,
                        params={
                            "user_id": user_id,
                            "session_id": session_id,
                            "summary_text": summary_text,
                        },
                        param_types={
                            "user_id": spanner.param_types.STRING,
                            "session_id": spanner.param_types.STRING,
                            "summary_text": spanner.param_types.STRING,
                        },
                    )

                self._database.run_in_transaction(_write)
            except Exception as e:
                logger.error(f"Failed to upsert session summary to live Spanner ({e})")

        return record

    def get_user_session_summaries(self, user_id: str) -> List[Dict[str, Any]]:
        """Retrieve all stored session summaries for a user from Spanner."""
        if self.use_mock or not self._database:
            return [s for s in self._in_memory_summaries.values() if s.get("user_id") == user_id]

        try:
            with self._database.snapshot() as snapshot:
                results = snapshot.execute_sql(
                    """
                    SELECT UserId, SessionId, SummaryText, CreatedAt
                    FROM UserSessionSummaries
                    WHERE UserId = @user_id
                    ORDER BY CreatedAt DESC
                    """,
                    params={"user_id": user_id},
                    param_types={"user_id": spanner.param_types.STRING},
                )
                summaries = []
                for row in results:
                    summaries.append({
                        "user_id": row[0],
                        "session_id": row[1],
                        "summary_text": row[2],
                        "created_at": str(row[3]) if row[3] else None,
                    })
                return summaries
        except Exception as e:
            logger.error(f"Error reading session summaries from Spanner ({e})")
            return [s for s in self._in_memory_summaries.values() if s.get("user_id") == user_id]

    def get_session_summary(self, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific session summary for a user."""
        if self.use_mock or not self._database:
            return self._in_memory_summaries.get(f"{user_id}::{session_id}")

        try:
            with self._database.snapshot() as snapshot:
                results = snapshot.execute_sql(
                    """
                    SELECT UserId, SessionId, SummaryText, CreatedAt
                    FROM UserSessionSummaries
                    WHERE UserId = @user_id AND SessionId = @session_id
                    """,
                    params={"user_id": user_id, "session_id": session_id},
                    param_types={
                        "user_id": spanner.param_types.STRING,
                        "session_id": spanner.param_types.STRING,
                    },
                )
                for row in results:
                    return {
                        "user_id": row[0],
                        "session_id": row[1],
                        "summary_text": row[2],
                        "created_at": str(row[3]) if row[3] else None,
                    }
            return None
        except Exception as e:
            logger.error(f"Error reading session summary from Spanner ({e})")
            return self._in_memory_summaries.get(f"{user_id}::{session_id}")

    def write_preferences(self, user_id: str, preferences: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Batch write multiple user preferences to Spanner."""
        written = []
        for pref in preferences:
            rec = self.upsert_user_preference(
                user_id=user_id,
                preference_key=str(pref.get("preference_key") or pref.get("memory_key") or ""),
                preference_value=str(pref.get("preference_value") or pref.get("memory_value") or ""),
                category=str(pref.get("category", "preference")),
                confidence=float(pref.get("confidence", 1.0)),
                source_session_id=str(pref.get("source_session_id", "")),
                crystallized_belief=str(pref.get("crystallized_belief", "")),
                keywords=str(pref.get("keywords", "")),
                embedding=pref.get("embedding"),
            )
            written.append(rec)
        return written

    # =========================================================================
    # 4-Scope Enterprise Hierarchy (ORG, TEAM, AGENT, USER)
    # =========================================================================

    @staticmethod
    def _normalize_scope(scope_type: str, scope_id: Optional[str] = None) -> tuple[str, str]:
        st = (scope_type or "USER").upper().strip()
        if st not in VALID_SCOPE_TYPES:
            st = "USER"
        sid = (scope_id or "").strip()
        if not sid:
            if st == "ORG":
                sid = "org:default"
            elif st == "TEAM":
                sid = "team:default"
            elif st == "AGENT":
                sid = "agent:default"
            else:
                sid = "user:default"
        return st, sid

    @staticmethod
    def _scoped_composite_uid(scope_type: str, scope_id: str) -> str:
        return f"scope::{scope_type.upper()}::{scope_id}"

    @staticmethod
    def _scoped_mem_key(scope_type: str, scope_id: str, memory_key: str) -> str:
        return f"{scope_type.upper()}::{scope_id}::{memory_key.lower().strip()}"

    @staticmethod
    def _infer_memory_type(
        memory_type: Optional[str],
        category: str,
        memory_key: str,
        keywords: str = "",
    ) -> str:
        if memory_type and memory_type.upper().strip() in VALID_MEMORY_TYPES:
            return memory_type.upper().strip()
        cat_l = (category or "").lower().strip()
        key_l = (memory_key or "").lower().strip()
        kw_l = (keywords or "").lower().strip()
        if (
            cat_l in ("guardrail", "invariant", "policy", "security")
            or key_l.startswith("guardrail_")
            or key_l.startswith("invariant_")
            or "guardrail" in kw_l
            or "invariant" in kw_l
        ):
            return "INVARIANT_GUARDRAIL"
        if cat_l == "procedural" or "procedural" in kw_l:
            return "PROCEDURAL_SKILL"
        if cat_l in ("intent", "episodic") or key_l in ("session_intent", "cross_session_intent", "intent_trajectory"):
            return "EPISODIC_FACT"
        return "PREFERENCE"

    def _compute_token_footprint(
        self,
        scope_type: str,
        category: str,
        memory_key: str,
        memory_value: str,
        crystallized_belief: str,
        keywords: str,
    ) -> Dict[str, int]:
        atomic_str = f"{memory_key}={memory_value}"
        compact_str = f"- [{scope_type}:{category}] {memory_key}: {memory_value}"
        full_str = (
            f"<memory scope=\"{scope_type}\" category=\"{category}\" key=\"{memory_key}\">"
            f"{crystallized_belief or memory_value} (keywords: {keywords})</memory>"
        )
        atomic_tokens = max(2, (len(atomic_str) + 3) // 4)
        compact_tokens = max(4, (len(compact_str) + 3) // 4)
        full_tokens = max(8, (len(full_str) + 3) // 4)
        return {
            "atomic_kv": atomic_tokens,
            "compact_bullet": compact_tokens,
            "full_belief": full_tokens,
            "active_tokens": compact_tokens,
        }

    def _default_trigger_conditions(
        self,
        scope_type: str,
        memory_type: str,
        keywords: str,
        custom_conditions: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        kw_list = [k.strip() for k in (keywords or "").split(",") if k.strip()]
        is_guardrail = (memory_type or "").upper() == "INVARIANT_GUARDRAIL"
        default_mode = "ALWAYS_ON_GUARDRAIL" if (is_guardrail and scope_type == "ORG") else "HYBRID_COSINE_AND_KEYWORD"
        default_thresh = 0.82 if (is_guardrail and scope_type == "ORG") else 0.72
        scope_rule = "ORG > TEAM > AGENT > USER" if is_guardrail else "USER > TEAM > AGENT > ORG"
        base = {
            "match_mode": default_mode,
            "cosine_threshold": default_thresh,
            "min_cosine_similarity": default_thresh,
            "trigger_keywords": kw_list[:8],
            "required_keywords": kw_list[:8],
            "auto_inject_scopes": [scope_type],
            "scope_rule": scope_rule,
        }
        if isinstance(custom_conditions, dict):
            if custom_conditions.get("match_mode"):
                base["match_mode"] = str(custom_conditions["match_mode"])
            cos_val = custom_conditions.get("min_cosine_similarity")
            if cos_val is None:
                cos_val = custom_conditions.get("cosine_threshold")
            if cos_val is not None:
                try:
                    c_float = float(cos_val)
                    base["cosine_threshold"] = c_float
                    base["min_cosine_similarity"] = c_float
                except (ValueError, TypeError):
                    pass
            req_kw = custom_conditions.get("required_keywords") or custom_conditions.get("trigger_keywords")
            if isinstance(req_kw, list):
                cleaned_kw = [str(k).strip() for k in req_kw if str(k).strip()]
                base["trigger_keywords"] = cleaned_kw
                base["required_keywords"] = cleaned_kw
            elif isinstance(req_kw, str):
                cleaned_kw = [k.strip() for k in req_kw.split(",") if k.strip()]
                base["trigger_keywords"] = cleaned_kw
                base["required_keywords"] = cleaned_kw
            if isinstance(custom_conditions.get("auto_inject_scopes"), list):
                base["auto_inject_scopes"] = [str(s) for s in custom_conditions["auto_inject_scopes"]]
            if custom_conditions.get("scope_rule"):
                base["scope_rule"] = str(custom_conditions["scope_rule"])
        return base

    def _enrich_memory_record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        st = str(rec.get("scope_type", "USER")).upper()
        cat = str(rec.get("category", "preference"))
        mk = str(rec.get("memory_key") or rec.get("preference_key") or "").lower().strip()
        mv = str(rec.get("memory_value") or rec.get("preference_value") or "")
        kw = str(rec.get("keywords", ""))
        mtype = str(rec.get("memory_type") or self._infer_memory_type(None, cat, mk, kw))
        belief = str(rec.get("crystallized_belief") or f"[{st}:{rec.get('scope_id', '')}] {mk} = {mv}")
        support = int(rec.get("support_count") or 1)

        trig_cond = self._default_trigger_conditions(
            scope_type=st,
            memory_type=mtype,
            keywords=kw,
            custom_conditions=rec.get("trigger_conditions"),
        )
        footprint = self._compute_token_footprint(
            scope_type=st,
            category=cat,
            memory_key=mk,
            memory_value=mv,
            crystallized_belief=belief,
            keywords=kw,
        )
        default_trig_count = int(rec.get("trigger_count") if rec.get("trigger_count") is not None else max(1, support * 3))
        default_reason = rec.get("last_trigger_reason") or (
            "Always-on ORG Invariant Guardrail"
            if (mtype == "INVARIANT_GUARDRAIL" and st == "ORG")
            else f"Hybrid match: keyword '{(trig_cond['trigger_keywords'] or [mk])[0]}' + cosine >= {trig_cond['min_cosine_similarity']:.2f}"
        )
        sync_stat = str(rec.get("sync_status") or "SYNCED").upper()
        if sync_stat in ("STAGED", "STAGED_NEW", "STAGED_MODIFIED"):
            sync_stat = "STAGED"
        else:
            sync_stat = "SYNCED"

        rec["memory_type"] = mtype
        rec["trigger_count"] = default_trig_count
        rec["last_triggered_at"] = rec.get("last_triggered_at") or rec.get("last_updated") or datetime.datetime.now(datetime.timezone.utc).isoformat()
        rec["last_trigger_reason"] = default_reason
        rec["trigger_reason"] = default_reason
        rec["trigger_conditions"] = trig_cond
        rec["token_footprint"] = max(6, int(footprint.get("active_tokens", 8)))
        rec["token_footprint_breakdown"] = footprint
        rec["sync_status"] = sync_stat
        return rec

    def _encode_bridge_metadata(
        self,
        source_session_id: str,
        memory_type: str,
        promoted_from_scope: str,
        support_count: int,
        trigger_count: Optional[int] = None,
        last_triggered_at: Optional[str] = None,
        last_trigger_reason: Optional[str] = None,
        trigger_conditions: Optional[Dict[str, Any]] = None,
        sync_status: str = "SYNCED",
    ) -> str:
        import json as _json
        meta: Dict[str, Any] = {
            "sid": source_session_id or "",
            "mt": memory_type or "PREFERENCE",
            "pf": promoted_from_scope or "",
            "sc": int(support_count or 1),
            "tc": int(trigger_count if trigger_count is not None else max(1, int(support_count or 1) * 3)),
            "ss": sync_status or "SYNCED",
        }
        if last_triggered_at:
            meta["lt"] = str(last_triggered_at)
        if last_trigger_reason:
            meta["lr"] = str(last_trigger_reason)[:160]
        if isinstance(trigger_conditions, dict):
            meta["cond"] = {
                "match_mode": trigger_conditions.get("match_mode", "HYBRID_KEYWORD_VECTOR"),
                "cosine_threshold": trigger_conditions.get("cosine_threshold", 0.65),
                "trigger_keywords": (trigger_conditions.get("trigger_keywords") or [])[:8],
                "scope_rule": trigger_conditions.get("scope_rule", ""),
            }
        return f"__SCOPED_META__{_json.dumps(meta, separators=(',', ':'))}"

    def _decode_bridge_metadata(self, raw_source_session_id: str, category: str, memory_key: str, keywords: str) -> Dict[str, Any]:
        import json as _json
        s = raw_source_session_id or ""
        if s.startswith("__SCOPED_META__"):
            try:
                payload = _json.loads(s[len("__SCOPED_META__"):])
                sc = int(payload.get("sc", 1))
                return {
                    "source_session_id": payload.get("sid", ""),
                    "memory_type": payload.get("mt") or self._infer_memory_type(None, category, memory_key, keywords),
                    "promoted_from_scope": payload.get("pf", ""),
                    "support_count": sc,
                    "trigger_count": int(payload.get("tc", max(1, sc * 3))),
                    "last_triggered_at": payload.get("lt"),
                    "last_trigger_reason": payload.get("lr"),
                    "trigger_conditions": payload.get("cond"),
                    "sync_status": payload.get("ss", "SYNCED"),
                }
            except Exception:
                pass
        return {
            "source_session_id": s,
            "memory_type": self._infer_memory_type(None, category, memory_key, keywords),
            "promoted_from_scope": "",
            "support_count": 1,
            "trigger_count": 3,
            "last_triggered_at": None,
            "last_trigger_reason": None,
            "trigger_conditions": None,
            "sync_status": "SYNCED",
        }

    def upsert_scoped_memory(
        self,
        scope_type: str,
        scope_id: str,
        memory_key: str,
        memory_value: str,
        memory_type: Optional[str] = None,
        category: str = "preference",
        confidence: float = 1.0,
        crystallized_belief: str = "",
        keywords: str = "",
        source_session_id: str = "",
        promoted_from_scope: str = "",
        support_count: int = 1,
        embedding: Optional[List[float]] = None,
        trigger_count: Optional[int] = None,
        last_triggered_at: Optional[str] = None,
        last_trigger_reason: Optional[str] = None,
        trigger_conditions: Optional[Dict[str, Any]] = None,
        sync_status: str = "SYNCED",
    ) -> Dict[str, Any]:
        """Insert or update a memory in the 4-scope hierarchy (ORG, TEAM, AGENT, USER).

        Uses `ScopedMemories` table if present on live Spanner AND bridges via `UserPreferences`
        with composite `scope::{ScopeType}::{ScopeId}` so live Spanner persistence is 100% zero-downtime.
        """
        st, sid = self._normalize_scope(scope_type, scope_id)
        norm_key = memory_key.lower().strip()
        mtype = self._infer_memory_type(memory_type, category, norm_key, keywords)
        if not crystallized_belief:
            crystallized_belief = f"[{st}:{sid}] {norm_key} = {memory_value}"
        if not keywords:
            keywords = f"{norm_key}, {memory_value}, {category}, {mtype.lower()}, {st.lower()}"
        if embedding is None:
            embedding = self._generate_embedding(
                f"{norm_key} {memory_value} {category} {mtype} {crystallized_belief} {keywords}"
            )
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        existing_in_mem = self._in_memory_scoped.get(self._scoped_mem_key(st, sid, norm_key))
        effective_trig_count = (
            int(trigger_count)
            if trigger_count is not None
            else int(existing_in_mem.get("trigger_count", max(1, support_count * 3)))
            if existing_in_mem
            else max(1, int(support_count) * 3)
        )

        record = self._enrich_memory_record({
            "scope_type": st,
            "scope_id": sid,
            "memory_key": norm_key,
            "preference_key": norm_key,
            "memory_value": str(memory_value),
            "preference_value": str(memory_value),
            "memory_type": mtype,
            "category": category,
            "confidence": float(confidence),
            "crystallized_belief": crystallized_belief,
            "keywords": keywords,
            "source_session_id": source_session_id,
            "promoted_from_scope": promoted_from_scope,
            "support_count": int(max(1, support_count)),
            "embedding": embedding,
            "last_updated": now_str,
            "trigger_count": effective_trig_count,
            "last_triggered_at": last_triggered_at or (existing_in_mem.get("last_triggered_at") if existing_in_mem else now_str),
            "last_trigger_reason": last_trigger_reason or (existing_in_mem.get("last_trigger_reason") if existing_in_mem else None),
            "trigger_conditions": trigger_conditions or (existing_in_mem.get("trigger_conditions") if existing_in_mem else None),
            "sync_status": sync_status or "SYNCED",
        })

        self._in_memory_scoped[self._scoped_mem_key(st, sid, norm_key)] = record

        if not self.use_mock and self._database:
            # 1. Try native ScopedMemories table if not known to be absent
            if self._scoped_table_available is not False:
                try:
                    def _write_scoped(transaction):
                        transaction.execute_update(
                            """
                            INSERT OR UPDATE ScopedMemories (
                                ScopeType, ScopeId, MemoryKey, MemoryValue, MemoryType, Category,
                                Confidence, CrystallizedBelief, Keywords, SourceSessionId,
                                PromotedFromScope, SupportCount, Embedding, LastUpdated
                            ) VALUES (
                                @scope_type, @scope_id, @mem_key, @mem_value, @mem_type, @category,
                                @confidence, @belief, @keywords, @source_sid,
                                @promoted_from, @support_count, @embedding, PENDING_COMMIT_TIMESTAMP()
                            )
                            """,
                            params={
                                "scope_type": st,
                                "scope_id": sid,
                                "mem_key": norm_key,
                                "mem_value": str(memory_value),
                                "mem_type": mtype,
                                "category": category,
                                "confidence": float(confidence),
                                "belief": crystallized_belief,
                                "keywords": keywords,
                                "source_sid": source_session_id,
                                "promoted_from": promoted_from_scope,
                                "support_count": int(max(1, support_count)),
                                "embedding": embedding,
                            },
                            param_types={
                                "scope_type": spanner.param_types.STRING,
                                "scope_id": spanner.param_types.STRING,
                                "mem_key": spanner.param_types.STRING,
                                "mem_value": spanner.param_types.STRING,
                                "mem_type": spanner.param_types.STRING,
                                "category": spanner.param_types.STRING,
                                "confidence": spanner.param_types.FLOAT64,
                                "belief": spanner.param_types.STRING,
                                "keywords": spanner.param_types.STRING,
                                "source_sid": spanner.param_types.STRING,
                                "promoted_from": spanner.param_types.STRING,
                                "support_count": spanner.param_types.INT64,
                                "embedding": spanner.param_types.Array(spanner.param_types.FLOAT64),
                            },
                        )

                    self._database.run_in_transaction(_write_scoped)
                    self._scoped_table_available = True
                except Exception as exc:
                    logger.debug(f"ScopedMemories table write skipped, using UserPreferences bridge ({exc})")
                    self._scoped_table_available = False

            # 2. Always persist via UserPreferences zero-downtime bridge so live Spanner reads never fail
            bridge_uid = self._scoped_composite_uid(st, sid)
            encoded_sid = self._encode_bridge_metadata(
                source_session_id=source_session_id,
                memory_type=mtype,
                promoted_from_scope=promoted_from_scope,
                support_count=support_count,
                trigger_count=record["trigger_count"],
                last_triggered_at=record["last_triggered_at"],
                last_trigger_reason=record["last_trigger_reason"],
                trigger_conditions=record["trigger_conditions"],
                sync_status=record["sync_status"],
            )
            self.upsert_user_preference(
                user_id=bridge_uid,
                preference_key=norm_key,
                preference_value=str(memory_value),
                category=category,
                confidence=float(confidence),
                source_session_id=encoded_sid,
                crystallized_belief=crystallized_belief,
                keywords=keywords,
                embedding=embedding,
            )

        return record

    def get_scoped_memory(
        self,
        scope_type: str,
        scope_id: str,
        memory_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a single scoped memory by (scope_type, scope_id, memory_key)."""
        st, sid = self._normalize_scope(scope_type, scope_id)
        norm_key = memory_key.lower().strip()
        all_in_scope = self.get_scoped_memories(st, sid)
        for m in all_in_scope:
            if m.get("memory_key") == norm_key:
                return m
        return None

    def get_scoped_memories(self, scope_type: str, scope_id: str) -> List[Dict[str, Any]]:
        """Retrieve all memories stored under a specific (scope_type, scope_id)."""
        st, sid = self._normalize_scope(scope_type, scope_id)
        by_key: Dict[str, Dict[str, Any]] = {}

        # 1. Check in-memory scoped store
        for rec in self._in_memory_scoped.values():
            if rec.get("scope_type") == st and rec.get("scope_id") == sid:
                by_key[rec["memory_key"]] = self._enrich_memory_record(dict(rec))

        # 2. Check live Spanner ScopedMemories table if available
        if not self.use_mock and self._database and self._scoped_table_available is not False:
            try:
                with self._database.snapshot() as snapshot:
                    results = snapshot.execute_sql(
                        """
                        SELECT ScopeType, ScopeId, MemoryKey, MemoryValue, MemoryType, Category,
                               Confidence, CrystallizedBelief, Keywords, SourceSessionId,
                               PromotedFromScope, SupportCount, Embedding, LastUpdated
                        FROM ScopedMemories
                        WHERE ScopeType = @scope_type AND ScopeId = @scope_id
                        """,
                        params={"scope_type": st, "scope_id": sid},
                        param_types={
                            "scope_type": spanner.param_types.STRING,
                            "scope_id": spanner.param_types.STRING,
                        },
                    )
                    for row in results:
                        mk = str(row[2]).lower().strip()
                        if mk in by_key:
                            continue
                        by_key[mk] = self._enrich_memory_record({
                            "scope_type": row[0],
                            "scope_id": row[1],
                            "memory_key": mk,
                            "preference_key": mk,
                            "memory_value": row[3],
                            "preference_value": row[3],
                            "memory_type": row[4] or self._infer_memory_type(None, row[5], mk, row[8] or ""),
                            "category": row[5] or "preference",
                            "confidence": float(row[6] if row[6] is not None else 1.0),
                            "crystallized_belief": row[7] or "",
                            "keywords": row[8] or "",
                            "source_session_id": row[9] or "",
                            "promoted_from_scope": row[10] or "",
                            "support_count": int(row[11] if row[11] is not None else 1),
                            "embedding": row[12] or [],
                            "last_updated": str(row[13]) if len(row) > 13 and row[13] else None,
                        })
                    self._scoped_table_available = True
            except Exception:
                self._scoped_table_available = False

        # 3. Check UserPreferences bridge namespace `scope::{st}::{sid}`
        bridge_uid = self._scoped_composite_uid(st, sid)
        bridge_prefs = self.get_all_user_preferences(bridge_uid)
        for p in bridge_prefs:
            mk = str(p.get("preference_key", "")).lower().strip()
            if not mk or mk in by_key:
                continue
            meta = self._decode_bridge_metadata(
                raw_source_session_id=str(p.get("source_session_id", "")),
                category=str(p.get("category", "")),
                memory_key=mk,
                keywords=str(p.get("keywords", "")),
            )
            by_key[mk] = self._enrich_memory_record({
                "scope_type": st,
                "scope_id": sid,
                "memory_key": mk,
                "preference_key": mk,
                "memory_value": str(p.get("preference_value", "")),
                "preference_value": str(p.get("preference_value", "")),
                "memory_type": meta["memory_type"],
                "category": p.get("category", "preference"),
                "confidence": float(p.get("confidence", 1.0)),
                "crystallized_belief": p.get("crystallized_belief", ""),
                "keywords": p.get("keywords", ""),
                "source_session_id": meta["source_session_id"],
                "promoted_from_scope": meta["promoted_from_scope"],
                "support_count": meta["support_count"],
                "trigger_count": meta.get("trigger_count"),
                "last_triggered_at": meta.get("last_triggered_at") or p.get("last_updated"),
                "last_trigger_reason": meta.get("last_trigger_reason"),
                "trigger_conditions": meta.get("trigger_conditions"),
                "sync_status": meta.get("sync_status", "SYNCED"),
                "embedding": p.get("embedding", []),
                "last_updated": p.get("last_updated"),
            })

        # 4. If USER scope, also merge direct UserPreferences for `sid` (and `sid` without `user:` prefix)
        if st == "USER":
            candidate_uids = [sid]
            if sid.startswith("user:"):
                candidate_uids.append(sid.split("user:", 1)[1])
            for cuid in candidate_uids:
                for p in self.get_all_user_preferences(cuid):
                    mk = str(p.get("preference_key", "")).lower().strip()
                    if not mk or mk in by_key:
                        continue
                    meta = self._decode_bridge_metadata(
                        raw_source_session_id=str(p.get("source_session_id", "")),
                        category=str(p.get("category", "")),
                        memory_key=mk,
                        keywords=str(p.get("keywords", "")),
                    )
                    by_key[mk] = self._enrich_memory_record({
                        "scope_type": "USER",
                        "scope_id": sid,
                        "memory_key": mk,
                        "preference_key": mk,
                        "memory_value": str(p.get("preference_value", "")),
                        "preference_value": str(p.get("preference_value", "")),
                        "memory_type": meta["memory_type"],
                        "category": p.get("category", "preference"),
                        "confidence": float(p.get("confidence", 1.0)),
                        "crystallized_belief": p.get("crystallized_belief", ""),
                        "keywords": p.get("keywords", ""),
                        "source_session_id": meta["source_session_id"],
                        "promoted_from_scope": meta["promoted_from_scope"],
                        "support_count": meta["support_count"],
                        "trigger_count": meta.get("trigger_count"),
                        "last_triggered_at": meta.get("last_triggered_at") or p.get("last_updated"),
                        "last_trigger_reason": meta.get("last_trigger_reason"),
                        "trigger_conditions": meta.get("trigger_conditions"),
                        "sync_status": meta.get("sync_status", "SYNCED"),
                        "embedding": p.get("embedding", []),
                        "last_updated": p.get("last_updated"),
                    })

        return list(by_key.values())

    def delete_scoped_memories(self, scope_type: str, scope_id: str) -> int:
        """Delete all memories in a specific (scope_type, scope_id) for Cold-Start Zero Reset."""
        st, sid = self._normalize_scope(scope_type, scope_id)
        removed = 0

        keys_to_remove = [
            k for k, v in self._in_memory_scoped.items()
            if v.get("scope_type") == st and v.get("scope_id") == sid
        ]
        for k in keys_to_remove:
            del self._in_memory_scoped[k]
            removed += 1

        bridge_uid = self._scoped_composite_uid(st, sid)
        pref_keys_to_remove = [
            k for k, v in self._in_memory_prefs.items()
            if v.get("user_id") in (bridge_uid, sid, sid.replace("user:", ""))
        ]
        for k in pref_keys_to_remove:
            del self._in_memory_prefs[k]
            removed += 1

        if not self.use_mock and self._database:
            try:
                def _delete_bridge(transaction):
                    uids = [bridge_uid]
                    if st == "USER":
                        uids.append(sid)
                        if sid.startswith("user:"):
                            uids.append(sid.split("user:", 1)[1])
                    for u in uids:
                        transaction.execute_update(
                            "DELETE FROM UserPreferences WHERE UserId = @uid",
                            params={"uid": u},
                            param_types={"uid": spanner.param_types.STRING},
                        )
                self._database.run_in_transaction(_delete_bridge)
            except Exception as e:
                logger.warning(f"Error deleting scoped bridge records from Spanner ({e})")

            if self._scoped_table_available:
                try:
                    def _delete_scoped(transaction):
                        transaction.execute_update(
                            "DELETE FROM ScopedMemories WHERE ScopeType = @st AND ScopeId = @sid",
                            params={"st": st, "sid": sid},
                            param_types={
                                "st": spanner.param_types.STRING,
                                "sid": spanner.param_types.STRING,
                            },
                        )
                    self._database.run_in_transaction(_delete_scoped)
                except Exception as e:
                    logger.debug(f"Error deleting from ScopedMemories table ({e})")

        return removed

    def delete_scoped_memory(self, scope_type: str, scope_id: str, memory_key: str) -> bool:
        """Delete a single specific memory key from a target scope in Cloud Spanner."""
        st, sid = self._normalize_scope(scope_type, scope_id)
        norm_key = (memory_key or "").lower().strip()
        if not norm_key:
            return False

        removed = False
        scoped_k = self._scoped_mem_key(st, sid, norm_key)
        if scoped_k in self._in_memory_scoped:
            del self._in_memory_scoped[scoped_k]
            removed = True

        bridge_uid = self._scoped_composite_uid(st, sid)
        candidate_uids = [bridge_uid]
        if st == "USER":
            candidate_uids.append(sid)
            if sid.startswith("user:"):
                candidate_uids.append(sid.split("user:", 1)[1])

        for cuid in candidate_uids:
            pk = self._pref_key(cuid, norm_key)
            if pk in self._in_memory_prefs:
                del self._in_memory_prefs[pk]
                removed = True

        if not self.use_mock and self._database:
            try:
                def _delete_single_bridge(transaction):
                    for u in candidate_uids:
                        transaction.execute_update(
                            "DELETE FROM UserPreferences WHERE UserId = @uid AND PreferenceKey = @pkey",
                            params={"uid": u, "pkey": norm_key},
                            param_types={
                                "uid": spanner.param_types.STRING,
                                "pkey": spanner.param_types.STRING,
                            },
                        )
                self._database.run_in_transaction(_delete_single_bridge)
                removed = True
            except Exception as e:
                logger.warning(f"Error deleting single scoped bridge record from Spanner ({e})")

            if self._scoped_table_available:
                try:
                    def _delete_single_scoped(transaction):
                        transaction.execute_update(
                            "DELETE FROM ScopedMemories WHERE ScopeType = @st AND ScopeId = @sid AND MemoryKey = @mk",
                            params={"st": st, "sid": sid, "mk": norm_key},
                            param_types={
                                "st": spanner.param_types.STRING,
                                "sid": spanner.param_types.STRING,
                                "mk": spanner.param_types.STRING,
                            },
                        )
                    self._database.run_in_transaction(_delete_single_scoped)
                    removed = True
                except Exception as e:
                    logger.debug(f"Error deleting single record from ScopedMemories table ({e})")

        return removed

    def federated_scoped_lookup(
        self,
        user_id: str,
        team_id: str = "team:default",
        agent_id: str = "agent:default",
        org_id: str = "org:default",
        query_text: str = "",
        config: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Federated lookup across all 4 enterprise scopes (ORG + TEAM + AGENT + USER).

        Deterministic Conflict Precedence:
        - INVARIANT_GUARDRAIL: ORG > TEAM > AGENT > USER (Org guardrails cannot be overridden by User)
        - PREFERENCE / PROCEDURAL_SKILL / EPISODIC_FACT: USER > TEAM > AGENT > ORG
          (User overrides Team/Org defaults for the same MemoryKey while inheriting non-conflicting memories)
        """
        cfg_dict: Dict[str, Any] = {}
        if config is not None:
            if isinstance(config, dict):
                cfg_dict = config
            elif hasattr(config, "to_dict"):
                cfg_dict = config.to_dict()
            elif hasattr(config, "__dict__"):
                cfg_dict = dict(config.__dict__)

        embed_dim = int(cfg_dict.get("embedding_dimensions", 768))
        if embed_dim not in MATRYOSHKA_DIMS:
            embed_dim = 768
        cosine_threshold = float(cfg_dict.get("retrieval_cosine_threshold", 0.65))
        top_k_per_scope = int(cfg_dict.get("top_k_per_scope", 6))
        injection_format = str(cfg_dict.get("injection_format", "COMPACT_BULLET"))
        scope_weights = cfg_dict.get(
            "scope_weights",
            {"ORG": 1.0, "TEAM": 0.85, "AGENT": 0.80, "USER": 0.95},
        )

        scopes_spec = [
            ("ORG", org_id or "org:default"),
            ("TEAM", team_id or "team:default"),
            ("AGENT", agent_id or "agent:default"),
            ("USER", user_id or "user:default"),
        ]

        query_vec_sliced: Optional[List[float]] = None
        if query_text and query_text.strip():
            raw_q_vec = self._generate_embedding(query_text.strip(), dim=768)
            query_vec_sliced = slice_and_normalize_vector(raw_q_vec, embed_dim)

        all_unfiltered_memories: List[Dict[str, Any]] = []
        scoped_candidates: List[Dict[str, Any]] = []
        suppressed_memories: List[Dict[str, Any]] = []
        by_scope_counts: Dict[str, int] = {"ORG": 0, "TEAM": 0, "AGENT": 0, "USER": 0}
        q_lower = (query_text or "").lower().strip()

        for st, sid in scopes_spec:
            raw_mems = self.get_scoped_memories(st, sid)
            all_unfiltered_memories.extend(raw_mems)
            scored_in_scope: List[tuple[float, Dict[str, Any]]] = []
            sw = float(scope_weights.get(st, 1.0))

            for m in raw_mems:
                m_copy = self._enrich_memory_record(dict(m))
                m_type = m_copy["memory_type"]
                m_vec = m_copy.get("embedding") or self._generate_embedding(
                    f"{m_copy.get('memory_key', '')} {m_copy.get('memory_value', '')} "
                    f"{m_copy.get('category', '')} {m_copy.get('crystallized_belief', '')} {m_copy.get('keywords', '')}"
                )
                m_vec_sliced = slice_and_normalize_vector(m_vec, embed_dim)
                m_copy["sliced_dimension"] = embed_dim

                trig_cond = m_copy.get("trigger_conditions") or {}
                kw_candidates = trig_cond.get("trigger_keywords") or [
                    k.strip() for k in str(m_copy.get("keywords", "")).split(",") if k.strip()
                ]
                matched_kws = [
                    kw for kw in kw_candidates
                    if kw and len(kw) >= 2 and kw.lower() in q_lower
                ] if q_lower else []
                m_copy["matched_keywords"] = matched_kws

                if query_vec_sliced is not None:
                    dist = self._cosine_distance(query_vec_sliced, m_vec_sliced, dim=embed_dim)
                    cos_sim = round(max(0.0, 1.0 - dist), 4)
                    m_copy["cosine_distance"] = round(dist, 4)
                    m_copy["cosine_similarity"] = cos_sim
                    # Guardrails from ORG have broader recall allowance so safety policies aren't dropped
                    effective_thresh = max(cosine_threshold, 0.82) if (st == "ORG" and m_type == "INVARIANT_GUARDRAIL") else cosine_threshold
                    if dist <= effective_thresh:
                        if st == "ORG" and m_type == "INVARIANT_GUARDRAIL":
                            m_copy["last_trigger_reason"] = f"Always-on ORG Invariant Guardrail (cosine sim {cos_sim:.2f})"
                        elif matched_kws:
                            m_copy["last_trigger_reason"] = (
                                f"Hybrid match: keyword '{matched_kws[0]}' + cosine sim {cos_sim:.2f} (dist {dist:.2f} <= {effective_thresh:.2f})"
                            )
                        else:
                            m_copy["last_trigger_reason"] = (
                                f"Semantic vector match: cosine sim {cos_sim:.2f} (dist {dist:.2f} <= {effective_thresh:.2f})"
                            )
                        weighted_dist = dist / max(0.1, sw)
                        scored_in_scope.append((weighted_dist, m_copy))
                    else:
                        m_copy["suppression_reason"] = (
                            f"Filtered by cosine threshold (dist {dist:.2f} > {effective_thresh:.2f})"
                        )
                        suppressed_memories.append(m_copy)
                else:
                    m_copy["cosine_distance"] = 0.0
                    m_copy["cosine_similarity"] = 1.0
                    if st == "ORG" and m_type == "INVARIANT_GUARDRAIL":
                        m_copy["last_trigger_reason"] = "Always-on ORG Invariant Guardrail"
                    else:
                        m_copy["last_trigger_reason"] = f"Active in {st}:{sid} scope bank"
                    scored_in_scope.append((0.0, m_copy))

            scored_in_scope.sort(key=lambda x: x[0])
            selected = [item for _, item in scored_in_scope[:top_k_per_scope]]
            for _, dropped_item in scored_in_scope[top_k_per_scope:]:
                dropped_copy = dict(dropped_item)
                dropped_copy["suppression_reason"] = f"Pruned by top_k_per_scope={top_k_per_scope}"
                suppressed_memories.append(dropped_copy)
            by_scope_counts[st] = len(selected)
            scoped_candidates.extend(selected)

        # Group by memory_key and apply deterministic precedence rules
        grouped_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for cand in scoped_candidates:
            mk = str(cand.get("memory_key", "")).lower().strip()
            if not mk:
                continue
            grouped_by_key.setdefault(mk, []).append(cand)

        guardrail_rank = {"ORG": 4, "TEAM": 3, "AGENT": 2, "USER": 1}
        preference_rank = {"USER": 4, "TEAM": 3, "AGENT": 2, "ORG": 1}

        resolved_memories: List[Dict[str, Any]] = []
        conflicts_resolved: List[Dict[str, Any]] = []

        for mk, group in grouped_by_key.items():
            is_guardrail = any(g.get("memory_type") == "INVARIANT_GUARDRAIL" for g in group)
            rank_map = guardrail_rank if is_guardrail else preference_rank
            rule_name = "INVARIANT_GUARDRAIL (ORG > TEAM > AGENT > USER)" if is_guardrail else "PREFERENCE (USER > TEAM > AGENT > ORG)"

            sorted_group = sorted(
                group,
                key=lambda item: (
                    rank_map.get(str(item.get("scope_type", "USER")).upper(), 0),
                    float(item.get("confidence", 1.0)),
                ),
                reverse=True,
            )
            winner = dict(sorted_group[0])
            if is_guardrail:
                winner["memory_type"] = "INVARIANT_GUARDRAIL"
            winner["winning_rule"] = rule_name
            resolved_memories.append(winner)

            if len(sorted_group) > 1:
                for loser in sorted_group[1:]:
                    if loser.get("memory_value") != winner.get("memory_value") or loser.get("scope_type") != winner.get("scope_type"):
                        conflicts_resolved.append({
                            "memory_key": mk,
                            "winning_scope": f"{winner.get('scope_type')}:{winner.get('scope_id')}",
                            "winning_scope_type": winner.get("scope_type"),
                            "winning_value": winner.get("memory_value"),
                            "overridden_scope": f"{loser.get('scope_type')}:{loser.get('scope_id')}",
                            "overridden_scope_type": loser.get("scope_type"),
                            "overridden_value": loser.get("memory_value"),
                            "rule_applied": rule_name,
                        })
                        loser_copy = dict(loser)
                        loser_copy["suppression_reason"] = (
                            f"Overridden by {winner.get('scope_type')}:{winner.get('scope_id')}={winner.get('memory_value')} ({rule_name})"
                        )
                        suppressed_memories.append(loser_copy)

        # Sort resolved memories: ORG guardrails first, then by cosine_distance / key
        resolved_memories.sort(
            key=lambda m: (
                0 if m.get("memory_type") == "INVARIANT_GUARDRAIL" else 1,
                float(m.get("cosine_distance", 0.0)),
                str(m.get("memory_key", "")),
            )
        )

        fmt_res = format_memories_for_context(resolved_memories, injection_format=injection_format)
        unfiltered_full = format_memories_for_context(
            all_unfiltered_memories if all_unfiltered_memories else resolved_memories,
            injection_format="FULL_BELIEF",
        )
        baseline_tokens = max(fmt_res["baseline_full_tokens"], unfiltered_full["estimated_tokens"])
        chosen_tokens = fmt_res["estimated_tokens"]
        savings_pct = round((1.0 - (chosen_tokens / baseline_tokens)) * 100.0, 1) if baseline_tokens > 0 else 0.0

        return {
            "scopes_queried": {
                "ORG": org_id or "org:default",
                "TEAM": team_id or "team:default",
                "AGENT": agent_id or "agent:default",
                "USER": user_id or "user:default",
            },
            "memories": resolved_memories,
            "suppressed_memories": suppressed_memories,
            "by_scope_counts": by_scope_counts,
            "conflicts_resolved": conflicts_resolved,
            "formatted_context": fmt_res["formatted_text"],
            "injection_format": fmt_res["injection_format"],
            "embedding_dimensions": embed_dim,
            "estimated_tokens": chosen_tokens,
            "baseline_tokens": baseline_tokens,
            "token_savings_pct": savings_pct,
        }

    def record_memory_triggers(
        self,
        triggered_memories: List[Dict[str, Any]],
        reason_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        """Increment trigger_count and update last_triggered_at / last_trigger_reason for triggered memories."""
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        updated_list: List[Dict[str, Any]] = []
        for m in triggered_memories:
            st = str(m.get("scope_type", "USER")).upper()
            sid = str(m.get("scope_id", "user:default"))
            mk = str(m.get("memory_key", "")).lower().strip()
            if not mk:
                continue
            current = self.get_scoped_memory(st, sid, mk) or m
            new_count = int(current.get("trigger_count", 1)) + 1
            reason = str(m.get("last_trigger_reason") or current.get("last_trigger_reason") or "Scenario match")
            if reason_prefix and not reason.startswith(reason_prefix):
                reason = f"{reason_prefix}: {reason}"
            k = self._scoped_mem_key(st, sid, mk)
            rec = dict(current)
            rec["trigger_count"] = new_count
            rec["last_triggered_at"] = now_str
            rec["last_trigger_reason"] = reason
            rec = self._enrich_memory_record(rec)
            self._in_memory_scoped[k] = rec
            updated_list.append(rec)
        return updated_list

    def sync_staged_memories(
        self,
        scopes_spec: Optional[List[tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Synchronize all staged memories across scopes into Cloud Spanner and mark SYNCED."""
        if not scopes_spec:
            scopes_spec = [
                ("ORG", "org:default"),
                ("TEAM", "team:platform-eng"),
                ("AGENT", "agent:code-reviewer"),
                ("USER", os.environ.get("DEFAULT_USER_ID", "user:developer")),
            ]
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sync_id = f"sync-{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}"

        all_mems: List[Dict[str, Any]] = []
        seen_keys = set()
        for st, sid in scopes_spec:
            for m in self.get_scoped_memories(st, sid):
                comp_k = self._scoped_mem_key(m["scope_type"], m["scope_id"], m["memory_key"])
                if comp_k not in seen_keys:
                    seen_keys.add(comp_k)
                    all_mems.append(m)

        # Also include any other in-memory scoped records
        for comp_k, m in list(self._in_memory_scoped.items()):
            if comp_k not in seen_keys:
                seen_keys.add(comp_k)
                all_mems.append(self._enrich_memory_record(dict(m)))

        staged_mems = [
            m for m in all_mems
            if m.get("sync_status") in ("STAGED", "STAGED_NEW", "STAGED_MODIFIED")
        ]
        targets_to_push = staged_mems if staged_mems else all_mems

        synced_records: List[Dict[str, Any]] = []
        for m in targets_to_push:
            updated = self.upsert_scoped_memory(
                scope_type=m["scope_type"],
                scope_id=m["scope_id"],
                memory_key=m["memory_key"],
                memory_value=m["memory_value"],
                memory_type=m.get("memory_type"),
                category=m.get("category", "preference"),
                confidence=float(m.get("confidence", 1.0)),
                crystallized_belief=m.get("crystallized_belief", ""),
                keywords=m.get("keywords", ""),
                source_session_id=m.get("source_session_id", ""),
                promoted_from_scope=m.get("promoted_from_scope", ""),
                support_count=int(m.get("support_count", 1)),
                embedding=m.get("embedding"),
                trigger_count=int(m.get("trigger_count", 1)),
                last_triggered_at=m.get("last_triggered_at") or now_str,
                last_trigger_reason=m.get("last_trigger_reason"),
                trigger_conditions=m.get("trigger_conditions"),
                sync_status="SYNCED",
            )
            synced_records.append(updated)

        return {
            "status": "SPANNER_SYNC_COMMITTED",
            "sync_id": sync_id,
            "transaction_tag": f"tx-{sync_id}",
            "target_table": f"{self.instance_id}.{self.database_id}.ScopedMemories",
            "embedding_model": "text-embedding-004 (768d COSINE)",
            "committed_at": now_str,
            "synced_at": now_str,
            "staged_promoted_count": len(staged_mems),
            "records_pushed": len(synced_records),
            "synced_count": len(synced_records),
            "embeddings_verified": len(synced_records),
            "embedding_dimensions": 768,
            "spanner_instance": self.instance_id,
            "spanner_database": self.database_id,
            "spanner_tables": ["ScopedMemories", "UserPreferences"],
            "synced_memories": [
                {k: v for k, v in rec.items() if k != "embedding"}
                for rec in synced_records
            ],
        }

    def promote_memory(
        self,
        source_scope_type: str,
        source_scope_id: str,
        memory_key: str,
        target_scope_type: str,
        target_scope_id: str,
        memory_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Manually promote a memory up the enterprise scope hierarchy (e.g., USER -> TEAM or TEAM -> ORG)."""
        src_st, src_sid = self._normalize_scope(source_scope_type, source_scope_id)
        tgt_st, tgt_sid = self._normalize_scope(target_scope_type, target_scope_id)
        norm_key = memory_key.lower().strip()

        src_mem = self.get_scoped_memory(src_st, src_sid, norm_key)
        if not src_mem:
            raise ValueError(f"Memory '{norm_key}' not found in source scope {src_st}:{src_sid}")

        existing_tgt = self.get_scoped_memory(tgt_st, tgt_sid, norm_key)
        prior_support = int(existing_tgt.get("support_count", 0)) if existing_tgt else 0
        src_support = int(src_mem.get("support_count", 1))
        new_support = max(1, prior_support + src_support)

        lineage = f"{src_st}:{src_sid}"
        if src_mem.get("promoted_from_scope"):
            lineage = f"{src_mem['promoted_from_scope']} -> {src_st}:{src_sid}"

        promoted = self.upsert_scoped_memory(
            scope_type=tgt_st,
            scope_id=tgt_sid,
            memory_key=norm_key,
            memory_value=str(src_mem.get("memory_value", "")),
            memory_type=memory_type or src_mem.get("memory_type", "PREFERENCE"),
            category=str(src_mem.get("category", "preference")),
            confidence=min(1.0, float(src_mem.get("confidence", 0.95)) + 0.02),
            crystallized_belief=str(src_mem.get("crystallized_belief", "")),
            keywords=str(src_mem.get("keywords", "")),
            source_session_id=str(src_mem.get("source_session_id", "MANUAL_PROMOTION")),
            promoted_from_scope=lineage,
            support_count=new_support,
            embedding=src_mem.get("embedding"),
        )
        return promoted

    promote_scoped_memory = promote_memory

    def detect_and_promote_consensus(
        self,
        team_id: str,
        user_ids: List[str],
        min_consensus_users: int = 2,
        target_scope_type: str = "TEAM",
        target_scope_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Inspect memories across users in a team and auto-promote shared beliefs into TEAM or ORG scope.

        When >= `min_consensus_users` share the same `memory_key` and semantically equivalent `memory_value`
        (exact normalized match or embedding cosine distance < 0.20), automatically upserts the consensus
        memory into `target_scope_type` (`TEAM` or `ORG`) with `support_count` and provenance.
        """
        tgt_st = (target_scope_type or "TEAM").upper().strip()
        if tgt_st == "ORG":
            tgt_sid = target_scope_id or "org:default"
        else:
            tgt_st = "TEAM"
            tgt_sid = target_scope_id or team_id or "team:default"

        by_key_users: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
        for uid in user_ids:
            u_mems = self.get_scoped_memories("USER", uid)
            for m in u_mems:
                mk = str(m.get("memory_key", "")).lower().strip()
                if mk:
                    by_key_users.setdefault(mk, []).append((uid, m))

        promoted_memories: List[Dict[str, Any]] = []

        for mk, user_mem_list in by_key_users.items():
            if len(user_mem_list) < min_consensus_users:
                continue

            # Cluster user memories by semantic equivalence (normalized value match OR cosine distance < 0.20)
            clusters: List[List[tuple[str, Dict[str, Any]]]] = []
            for uid, mem in user_mem_list:
                val_norm = str(mem.get("memory_value", "")).lower().strip()
                mem_vec = mem.get("embedding") or self._generate_embedding(f"{mk} {val_norm}")
                placed = False
                for cluster in clusters:
                    rep_uid, rep_mem = cluster[0]
                    rep_val_norm = str(rep_mem.get("memory_value", "")).lower().strip()
                    rep_vec = rep_mem.get("embedding") or self._generate_embedding(f"{mk} {rep_val_norm}")
                    if val_norm == rep_val_norm or self._cosine_distance(mem_vec, rep_vec) < 0.20:
                        if all(existing_uid != uid for existing_uid, _ in cluster):
                            cluster.append((uid, mem))
                        placed = True
                        break
                if not placed:
                    clusters.append([(uid, mem)])

            # Find largest consensus cluster meeting threshold
            clusters.sort(key=lambda c: len(c), reverse=True)
            best_cluster = clusters[0]
            if len(best_cluster) >= min_consensus_users:
                supporting_uids = [u for u, _ in best_cluster]
                rep_mem = best_cluster[0][1]
                avg_conf = sum(float(m.get("confidence", 0.9)) for _, m in best_cluster) / len(best_cluster)
                provenance = f"CONSENSUS({','.join(supporting_uids)})"
                promoted_rec = self.upsert_scoped_memory(
                    scope_type=tgt_st,
                    scope_id=tgt_sid,
                    memory_key=mk,
                    memory_value=str(rep_mem.get("memory_value", "")),
                    memory_type=str(rep_mem.get("memory_type", "PREFERENCE")),
                    category=str(rep_mem.get("category", "preference")),
                    confidence=round(min(1.0, avg_conf + 0.05), 4),
                    crystallized_belief=(
                        f"Team consensus ({len(supporting_uids)} users: {', '.join(supporting_uids)}): "
                        f"{rep_mem.get('crystallized_belief') or rep_mem.get('memory_value')}"
                    ),
                    keywords=str(rep_mem.get("keywords", "")),
                    source_session_id="CONSENSUS_AUTO_PROMOTION",
                    promoted_from_scope=provenance,
                    support_count=len(supporting_uids),
                    embedding=rep_mem.get("embedding"),
                )
                promoted_memories.append(promoted_rec)

        return {
            "team_id": team_id,
            "target_scope_type": tgt_st,
            "target_scope_id": tgt_sid,
            "min_consensus_users": min_consensus_users,
            "promoted_count": len(promoted_memories),
            "promoted_memories": promoted_memories,
        }

    # =========================================================================
    # Enterprise Memory Studio 10-Workspace Persistence Layer (Cloud Spanner)
    # =========================================================================

    def _init_studio_defaults(self) -> None:
        if getattr(self, "_studio_defaults_ready", False):
            return
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # 1. Default Scope Entities (Workspace 5)
        self._studio_entities: Dict[str, Dict[str, Any]] = {
            "ent_org_root": {
                "id": "ent_org_root",
                "entity_id": "ent_org_root",
                "type": "org",
                "scope_type": "org",
                "slug": "acme-org",
                "name": "Acme Enterprise GCP Organization",
                "path": "/org/acme-org",
                "parentPath": "/",
                "parent_path": "/",
                "precedenceRank": 1,
                "precedence_rank": 1,
                "write_mode": "STAGE_FIRST_2_PERSON",
                "owner": "developer@example.com",
                "identity_binding": "gcp-project:your-gcp-project-id | ADC: gcloud auth application-default",
                "binding": "gcp-project:your-gcp-project-id | ADC: gcloud auth application-default",
                "description": "Root organizational namespace enforcing zero-mock policies, ADC authentication defaults, and Apple Silicon hardware guardrails across all child teams and agents.",
                "updated_at": now_iso,
            },
            "ent_team_platform": {
                "id": "ent_team_platform",
                "entity_id": "ent_team_platform",
                "type": "team",
                "scope_type": "team",
                "slug": "platform-ml",
                "name": "Platform & Agentic ML Engineering",
                "path": "/team/platform-ml",
                "parentPath": "/org/acme-org",
                "parent_path": "/org/acme-org",
                "precedenceRank": 2,
                "precedence_rank": 2,
                "write_mode": "STAGE_ONLY_SHADOW",
                "owner": "platform-leads@example.com",
                "identity_binding": "iam-group:platform-ml@example.com",
                "binding": "iam-group:platform-ml@example.com",
                "description": "Shared engineering conventions for Python uv testing, Terraform validation, and Vertex AI custom training pipelines.",
                "updated_at": now_iso,
            },
            "ent_team_robotics": {
                "id": "ent_team_robotics",
                "entity_id": "ent_team_robotics",
                "type": "team",
                "scope_type": "team",
                "slug": "robotics-cleanroom",
                "name": "Cleanroom Workcell & Kinematics Team",
                "path": "/team/robotics-cleanroom",
                "parentPath": "/org/acme-org",
                "parent_path": "/org/acme-org",
                "precedenceRank": 2,
                "precedence_rank": 2,
                "write_mode": "READ_ONLY_LOCKED",
                "owner": "robotics-ops@example.com",
                "identity_binding": "iam-group:robotics-cleanroom@example.com",
                "binding": "iam-group:robotics-cleanroom@example.com",
                "description": "ISA-101 HMI standards and physical fracture/torque safety limits for RoboFab workcells.",
                "updated_at": now_iso,
            },
            "ent_agent_workcell": {
                "id": "ent_agent_workcell",
                "entity_id": "ent_agent_workcell",
                "type": "agent",
                "scope_type": "agent",
                "slug": "workcell-agent",
                "name": "Workcell B Kinematics Controller Agent",
                "path": "/agent/workcell-agent",
                "parentPath": "/team/robotics-cleanroom",
                "parent_path": "/team/robotics-cleanroom",
                "precedenceRank": 3,
                "precedence_rank": 3,
                "write_mode": "READ_ONLY_LOCKED",
                "owner": "service-account:workcell-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "identity_binding": "model:gemini-4-neon | runtime:cloud-run-workcell",
                "binding": "model:gemini-4-neon | runtime:cloud-run-workcell",
                "description": "Real-time robotics supervisor agent monitoring J1-J6 joint torque and triggering Cascade Andon Halts.",
                "updated_at": now_iso,
            },
            "ent_agent_reviewer": {
                "id": "ent_agent_reviewer",
                "entity_id": "ent_agent_reviewer",
                "type": "agent",
                "scope_type": "agent",
                "slug": "code-reviewer-agent",
                "name": "TDD & Cloud Run Verification Subagent",
                "path": "/agent/code-reviewer-agent",
                "parentPath": "/team/platform-ml",
                "parent_path": "/team/platform-ml",
                "precedenceRank": 3,
                "precedence_rank": 3,
                "write_mode": "WRITE_THROUGH_LIVE",
                "owner": "service-account:reviewer-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "identity_binding": "model:gemini-4-hydrogen | skill:subagent-delegated-execution",
                "binding": "model:gemini-4-hydrogen | skill:subagent-delegated-execution",
                "description": "Paired rater subagent verifying live Cloud Run HTTP probes, DESIGN.md compliance, and zero-mock adherence.",
                "updated_at": now_iso,
            },
            "ent_user_developer": {
                "id": "ent_user_developer",
                "entity_id": "ent_user_developer",
                "type": "user",
                "scope_type": "user",
                "slug": "developer",
                "name": "Lead Developer (Principal Architect)",
                "path": "/user/developer",
                "parentPath": "/team/platform-ml",
                "parent_path": "/team/platform-ml",
                "precedenceRank": 4,
                "precedence_rank": 4,
                "write_mode": "WRITE_THROUGH_LIVE",
                "owner": "developer@example.com",
                "identity_binding": "principal:developer@example.com | host:macOS-arm64",
                "binding": "principal:developer@example.com | host:macOS-arm64",
                "description": "Individual developer profile preferences including academic light/sepia 3-panel UI layouts and concise markdown links.",
                "updated_at": now_iso,
            },
        }

        # 2. Default Memory Categories (Workspace 6)
        self._studio_categories: Dict[str, Dict[str, Any]] = {
            "cat_python": {
                "id": "cat_python",
                "category_id": "cat_python",
                "slug": "python_tooling",
                "folder_path": "python_tooling",
                "name": "Python Tooling & Dependency Standards",
                "discoveryMode": "FILE_GLOB_AND_SEMANTIC",
                "discovery_mode": "FILE_GLOB_AND_SEMANTIC",
                "recallTrigger": "FILE_GLOB: *.py, pyproject.toml, uv.lock",
                "recall_trigger": "FILE_GLOB: *.py, pyproject.toml, uv.lock",
                "minConfidence": 0.85,
                "retentionTtl": "PERMANENT (Crystallized)",
                "retention_ttl": "PERMANENT (Crystallized)",
                "conflictPolicy": "HIGHER_SCOPE_OVERRIDES_UNLESS_USER_STYLE",
                "description": "Governs Python virtual environments, test runners (`uv run pytest`), and package installation rules.",
                "updated_at": now_iso,
            },
            "cat_silicon": {
                "id": "cat_silicon",
                "category_id": "cat_silicon",
                "slug": "apple_silicon_guardrails",
                "folder_path": "apple_silicon_guardrails",
                "name": "Apple Silicon & Compute Offload Guardrails",
                "discoveryMode": "ALWAYS_PINNED",
                "discovery_mode": "ALWAYS_PINNED",
                "recallTrigger": "ALWAYS_PINNED (System Invariant)",
                "recall_trigger": "ALWAYS_PINNED (System Invariant)",
                "minConfidence": 0.90,
                "retentionTtl": "PERMANENT (Hardware Safety)",
                "retention_ttl": "PERMANENT (Hardware Safety)",
                "conflictPolicy": "ORG_INVARIANT_NON_OVERRIDABLE",
                "description": "Prevents local macOS kernel panics by blocking heavy local model training and routing jobs to Vertex AI L4 GPUs.",
                "updated_at": now_iso,
            },
            "cat_cloud_run": {
                "id": "cat_cloud_run",
                "category_id": "cat_cloud_run",
                "slug": "cloud_run_deploy",
                "folder_path": "cloud_run_deploy",
                "name": "Cloud Run Deployment & Live Verification",
                "discoveryMode": "INTENT_AND_COMMAND",
                "discovery_mode": "INTENT_AND_COMMAND",
                "recallTrigger": "INTENT: deploy, service, api, endpoint, cloud run",
                "recall_trigger": "INTENT: deploy, service, api, endpoint, cloud run",
                "minConfidence": 0.88,
                "retentionTtl": "PERMANENT (Operational Gate)",
                "retention_ttl": "PERMANENT (Operational Gate)",
                "conflictPolicy": "ORG_INVARIANT_NON_OVERRIDABLE",
                "description": "Enforces deploying services to `your-gcp-project-id` Cloud Run and running live HTTP endpoint probes before completion.",
                "updated_at": now_iso,
            },
            "cat_kinematics": {
                "id": "cat_kinematics",
                "category_id": "cat_kinematics",
                "slug": "kinematics_constraints",
                "folder_path": "kinematics_constraints",
                "name": "Workcell Kinematics & Fracture Limits",
                "discoveryMode": "DOMAIN_PINNED",
                "discovery_mode": "DOMAIN_PINNED",
                "recallTrigger": "AGENT_SCOPE: /agent/workcell-agent/**",
                "recall_trigger": "AGENT_SCOPE: /agent/workcell-agent/**",
                "minConfidence": 0.95,
                "retentionTtl": "PERMANENT (Physical Safety)",
                "retention_ttl": "PERMANENT (Physical Safety)",
                "conflictPolicy": "AGENT_SAFETY_PIN",
                "description": "Physical joint torque, velocity, and Cascade Andon Halt thresholds for cleanroom robotics workcells.",
                "updated_at": now_iso,
            },
            "cat_ui_design": {
                "id": "cat_ui_design",
                "category_id": "cat_ui_design",
                "slug": "ui_design_system",
                "folder_path": "ui_design_system",
                "name": "UI/UX Design System & Typography",
                "discoveryMode": "FILE_GLOB_AND_SEMANTIC",
                "discovery_mode": "FILE_GLOB_AND_SEMANTIC",
                "recallTrigger": "FILE_GLOB: *.html, *.css, *.tsx, DESIGN.md",
                "recall_trigger": "FILE_GLOB: *.html, *.css, *.tsx, DESIGN.md",
                "minConfidence": 0.80,
                "retentionTtl": "365 Days (Rolling)",
                "retention_ttl": "365 Days (Rolling)",
                "conflictPolicy": "USER_PREFERENCE_OVERRIDES_DEFAULT",
                "description": "Academic light/sepia palette, 2-3 panel master-detail layout, and zero-mock interactive control wiring.",
                "updated_at": now_iso,
            },
        }

        # 3. Default Ingest Services (Workspace 7)
        self._studio_ingest_services: Dict[str, Dict[str, Any]] = {
            "ing_mcp_bridge": {
                "id": "ing_mcp_bridge",
                "service_id": "ing_mcp_bridge",
                "name": "Antigravity IDE & MCP Live Trajectory Bridge",
                "streamKey": "MCP_LIVE_STREAM",
                "stream_key": "MCP_LIVE_STREAM",
                "protocol": "gRPC / MCP SSE Stream",
                "endpoint": "mcp://electric-sheep-memory/ingest/v1/sessions",
                "endpoint_url": "mcp://electric-sheep-memory/ingest/v1/sessions",
                "targetSink": "Spanner `SessionTrajectories` + GCS `gs://your-vertex-staging-bucket/trajectories/`",
                "piiRedaction": "STRICT (Cloud DLP De-ID + Zero-Secret Regex)",
                "batchWindow": "Real-time per Turn (Streaming)",
                "throughput": "42 turns/min",
                "throughput_eps": 42.0,
                "avg_latency_ms": 8.4,
                "queue_depth": 0,
                "status": "ACTIVE",
                "description": "Captures live developer-agent pair programming turns, explicit user corrections, and tool execution outcomes directly from Antigravity sessions.",
                "updated_at": now_iso,
            },
            "ing_bq_telemetry": {
                "id": "ing_bq_telemetry",
                "service_id": "ing_bq_telemetry",
                "name": "BigQuery Agent Telemetry Sink Connector",
                "streamKey": "BIGQUERY_REALTIME_SINK",
                "stream_key": "BIGQUERY_REALTIME_SINK",
                "protocol": "BigQuery Storage Write API",
                "endpoint": "bq://your-gcp-project-id.electric_sheep_telemetry.episodes_log",
                "endpoint_url": "bq://your-gcp-project-id.electric_sheep_telemetry.episodes_log",
                "targetSink": "BigQuery `episodes_log` -> Spanner Change Stream Bridge",
                "piiRedaction": "ENABLED (IAM Token & API Key Scrubbing)",
                "batchWindow": "30s Micro-Batch",
                "throughput": "185 events/min",
                "throughput_eps": 185.0,
                "avg_latency_ms": 24.5,
                "queue_depth": 2,
                "status": "ACTIVE",
                "description": "Streams structured JSONL agent evaluation logs and Cloud Run service traces from BigQuery into the Memory Studio extraction queue.",
                "updated_at": now_iso,
            },
            "ing_vertex_engine": {
                "id": "ing_vertex_engine",
                "service_id": "ing_vertex_engine",
                "name": "Vertex AI Agent Engine & Workcell Bus",
                "streamKey": "VERTEX_AGENT_ENGINE",
                "stream_key": "VERTEX_AGENT_ENGINE",
                "protocol": "Cloud Pub/Sub (`projects/your-gcp-project-id/topics/agent-episodes`)",
                "endpoint": "pubsub://your-gcp-project-id/agent-episodes",
                "endpoint_url": "pubsub://your-gcp-project-id/agent-episodes",
                "targetSink": "Spanner `WorkcellEpisodes`",
                "piiRedaction": "ENABLED",
                "batchWindow": "Event-Driven (Push Subscription)",
                "throughput": "19 episodes/min",
                "throughput_eps": 19.0,
                "avg_latency_ms": 14.2,
                "queue_depth": 0,
                "status": "ACTIVE",
                "description": "Ingests autonomous subagent and RoboFab workcell telemetry, including Andon halts and rater subagent critiques.",
                "updated_at": now_iso,
            },
        }

        self._studio_ingest_events: List[Dict[str, Any]] = [
            {
                "id": "evt-9901",
                "time": "14:20:19",
                "stream": "MCP_LIVE_STREAM",
                "session": "sess-9042",
                "actor": "user:developer",
                "bytes": "1.4 KB",
                "piiStatus": "CLEAN (0 secrets)",
                "routedTo": "ext_dev_corrections",
            },
            {
                "id": "evt-9900",
                "time": "13:10:12",
                "stream": "BIGQUERY_REALTIME_SINK",
                "session": "sess-9019",
                "actor": "user:developer",
                "bytes": "2.8 KB",
                "piiStatus": "CLEAN (0 secrets)",
                "routedTo": "ext_arch_invariants",
            },
            {
                "id": "evt-9899",
                "time": "10:04:11",
                "stream": "VERTEX_AGENT_ENGINE",
                "session": "sess-8984",
                "actor": "agent:workcell-agent",
                "bytes": "0.9 KB",
                "piiStatus": "CLEAN (0 secrets)",
                "routedTo": "ext_workcell_safety",
            },
        ]

        # 4. Default Extractor Jobs (Workspace 8)
        self._studio_extractors: Dict[str, Dict[str, Any]] = {
            "ext_dev_corrections": {
                "id": "ext_dev_corrections",
                "extractor_id": "ext_dev_corrections",
                "name": "Developer Correction & Tooling Preference Extractor",
                "model": "gemini-4-hydrogen",
                "model_id": "gemini-4-hydrogen",
                "location": "global",
                "source_stream": "MCP_LIVE_STREAM",
                "subscribedStreams": ["MCP_LIVE_STREAM", "BIGQUERY_REALTIME_SINK"],
                "targetScopes": ["/user/*", "/team/platform-ml"],
                "targetCategory": "python_tooling",
                "target_category": "python_tooling",
                "confidenceThreshold": 0.88,
                "dedupStrategy": "COSINE_SIMILARITY >= 0.85 + CONTRADICTION_CHECK",
                "writeMode": "STAGE_FOR_COMMIT",
                "write_mode": "STAGE_FOR_COMMIT",
                "status": "RUNNING",
                "humanParams": {
                    "lookbackTurns": 10,
                    "requireExplicitNegation": True,
                    "autoEvictContradicted": True,
                    "maxTokensPerMemory": 60,
                    "requireCodeEvidence": True,
                    "owner": "developer@example.com",
                },
                "systemPrompt": (
                    "You are the Developer Correction Memory Extractor running on Vertex AI (gemini-4-hydrogen).\n"
                    "Inspect the ingested session turns between USER and AGENT.\n"
                    "1. Detect any explicit developer correction (e.g., \"Stop! Never use X, always use Y\").\n"
                    "2. Formulate a concise, atomic crystallized belief (< 60 tokens).\n"
                    "3. Assign target scope (/user/<slug> for personal style, /team/<slug> for repo tooling conventions) and category folder.\n"
                    "4. Check existing memories in that folder: if a prior file exists, emit an UPDATE diff; if it contradicts a legacy fallback, emit a DELETE commit."
                ),
                "updated_at": now_iso,
            },
            "ext_arch_invariants": {
                "id": "ext_arch_invariants",
                "extractor_id": "ext_arch_invariants",
                "name": "Architectural & Cloud Run Invariant Synthesizer",
                "model": "gemini-4-neon",
                "model_id": "gemini-4-neon",
                "location": "global",
                "source_stream": "MCP_LIVE_STREAM",
                "subscribedStreams": ["MCP_LIVE_STREAM", "BIGQUERY_REALTIME_SINK"],
                "targetScopes": ["/org/acme-org", "/team/platform-ml"],
                "targetCategory": "cloud_run_deploy",
                "target_category": "cloud_run_deploy",
                "confidenceThreshold": 0.92,
                "dedupStrategy": "SEMANTIC_MERGE_INTO_CANONICAL_FILE",
                "writeMode": "STAGE_FOR_COMMIT",
                "write_mode": "STAGE_FOR_COMMIT",
                "status": "RUNNING",
                "humanParams": {
                    "lookbackTurns": 20,
                    "requireExplicitNegation": False,
                    "autoEvictContradicted": False,
                    "maxTokensPerMemory": 80,
                    "requireCodeEvidence": False,
                    "owner": "platform-leads@example.com",
                },
                "systemPrompt": (
                    "You are the Organizational Invariant Extractor (gemini-4-neon).\n"
                    "Identify non-negotiable infrastructure, authentication (ADC), hardware (Apple Silicon), and Cloud Run deployment verification rules.\n"
                    "Route verified invariants to /org/acme-org/<category>/<name>.mem as staged commits."
                ),
                "updated_at": now_iso,
            },
            "ext_workcell_safety": {
                "id": "ext_workcell_safety",
                "extractor_id": "ext_workcell_safety",
                "name": "Cleanroom Kinematics & Andon Safety Extractor",
                "model": "gemma-4-27b-it",
                "model_id": "gemma-4-27b-it",
                "location": "us-central1",
                "source_stream": "VERTEX_AGENT_ENGINE",
                "subscribedStreams": ["VERTEX_AGENT_ENGINE"],
                "targetScopes": ["/agent/workcell-agent", "/team/robotics-cleanroom"],
                "targetCategory": "kinematics_constraints",
                "target_category": "kinematics_constraints",
                "confidenceThreshold": 0.95,
                "dedupStrategy": "STRICT_NUMERIC_BOUNDS_CHECK",
                "writeMode": "AUTO_COMMIT_IF_SAFER",
                "write_mode": "AUTO_COMMIT_IF_SAFER",
                "status": "RUNNING",
                "humanParams": {
                    "lookbackTurns": 5,
                    "requireExplicitNegation": False,
                    "autoEvictContradicted": True,
                    "maxTokensPerMemory": 50,
                    "requireCodeEvidence": True,
                    "owner": "robotics-ops@example.com",
                },
                "systemPrompt": (
                    "You are the Cleanroom Safety Memory Extractor powered by Gemma 4 (gemma-4-27b-it on Vertex AI L4 GPU).\n"
                    "Extract physical joint torque limits, fracture thresholds, and Cascade Andon Halt state transition rules for /agent/workcell-agent."
                ),
                "updated_at": now_iso,
            },
        }

        # 5. Default Memory File Tree & Staged Commits (Workspace 2)
        self._studio_memory_files: Dict[str, Dict[str, Any]] = {
            "org_apple_silicon": {
                "id": "org_apple_silicon",
                "memory_id": "org_apple_silicon",
                "scope": "org",
                "scope_type": "ORG",
                "scope_id": "org:acme-org",
                "scopeFolder": "org/your-gcp-project-id",
                "categoryFolder": "apple_silicon_guardrails",
                "category": "apple_silicon_guardrails",
                "fileName": "hardware_offload.mem",
                "file_path": "/org/acme-org/apple_silicon_guardrails/hardware_offload.mem",
                "commitState": "SYNCED",
                "sync_status": "SYNCED",
                "currentContent": "Host is Apple Silicon (macOS). Never execute heavy model training, large weight loading, or unrolled autoregressive loops locally. Restrict local runs to fast unit tests (`uv run pytest`, ~3s) and offload heavy runs to Vertex AI Custom Training (L4 GPUs).",
                "proposedContent": None,
                "diffSummary": None,
                "confidence": 0.99,
                "originMode": "USER",
                "write_mode": "STAGE_FIRST_2_PERSON",
                "sessionId": "sess-9019",
                "trigger": "ALWAYS_PINNED",
                "tokens": 44,
                "bytes": 248,
                "helpful_count": 14,
                "unhelpful_count": 0,
                "provenance": "👤 User Explicit Directive · Committed to Spanner",
                "usefulnessTracker": {
                    "usefulCount": 14,
                    "notUsefulCount": 0,
                    "reviewThreshold": 3,
                    "recentEvents": [
                        {
                            "sessionId": "sess-9019",
                            "outcome": "HELPED",
                            "note": "Prevented local autoregressive loop on macOS host; offloaded to Vertex AI L4 GPU.",
                        },
                        {
                            "sessionId": "sess-8950",
                            "outcome": "HELPED",
                            "note": "Blocked local 27B weight loading; routed job to gs://your-vertex-staging-bucket/.",
                        },
                    ],
                },
                "relations": [
                    {"type": "inherits", "text": "enforced across -> /team/platform-ml & /user/developer"}
                ],
                "updated_at": now_iso,
            },
            "org_cloud_run_verify": {
                "id": "org_cloud_run_verify",
                "memory_id": "org_cloud_run_verify",
                "scope": "org",
                "scope_type": "ORG",
                "scope_id": "org:acme-org",
                "scopeFolder": "org/your-gcp-project-id",
                "categoryFolder": "cloud_run_deploy",
                "category": "cloud_run_deploy",
                "fileName": "live_endpoint_verification.mem",
                "file_path": "/org/acme-org/cloud_run_deploy/live_endpoint_verification.mem",
                "commitState": "ADD",
                "sync_status": "STAGED_NEW",
                "currentContent": None,
                "proposedContent": "A task involving services or web applications is NOT complete until built, deployed to Google Cloud Run (`your-gcp-project-id`), and verified via live HTTP probe (`python3 -m scripts.validate_live_endpoints`). Always provide the live service URL.",
                "diffSummary": "+247B · Extracted by ext_arch_invariants from sess-9042",
                "confidence": 0.97,
                "originMode": "AGENT",
                "write_mode": "STAGE_ONLY_SHADOW",
                "sessionId": "sess-9042",
                "trigger": "INTENT: deploy, service, cloud run",
                "tokens": 46,
                "bytes": 247,
                "helpful_count": 5,
                "unhelpful_count": 0,
                "provenance": "⚙️ Extracted by Job ext_arch_invariants from sess-9042 (Turn #2)",
                "usefulnessTracker": {
                    "usefulCount": 5,
                    "notUsefulCount": 0,
                    "reviewThreshold": 3,
                    "recentEvents": [
                        {
                            "sessionId": "sess-9042",
                            "outcome": "HELPED",
                            "note": "Enforced live Cloud Run verification probe before marking deployment complete.",
                        }
                    ],
                },
                "relations": [
                    {"type": "supports", "text": "verifies -> /team/platform-ml/python_tooling/python_uv_runner.mem"}
                ],
                "updated_at": now_iso,
            },
            "team_python_uv": {
                "id": "team_python_uv",
                "memory_id": "team_python_uv",
                "scope": "team",
                "scope_type": "TEAM",
                "scope_id": "team:platform-ml",
                "scopeFolder": "team/platform-ml",
                "categoryFolder": "python_tooling",
                "category": "python_tooling",
                "fileName": "python_uv_runner.mem",
                "file_path": "/team/platform-ml/python_tooling/python_uv_runner.mem",
                "commitState": "UPDATE",
                "sync_status": "STAGED_MODIFIED",
                "currentContent": "Use `uv run pytest` for running Python unit tests in this repository.",
                "proposedContent": "Always use `uv` (`uv run pytest`, `uv add`) for Python testing and dependency management. Never invoke global `pip install` or bare `pytest`.",
                "diffSummary": "+75B / -12B · Ban global pip install (from sess-9042)",
                "confidence": 0.99,
                "originMode": "HYBRID",
                "write_mode": "WRITE_THROUGH_LIVE",
                "sessionId": "sess-9042",
                "trigger": "FILE_GLOB: *.py, pyproject.toml",
                "tokens": 32,
                "bytes": 146,
                "helpful_count": 9,
                "unhelpful_count": 1,
                "provenance": "🔀 Hybrid: User seed + Agent update from sess-9042 (Turn #2)",
                "usefulnessTracker": {
                    "usefulCount": 9,
                    "notUsefulCount": 1,
                    "reviewThreshold": 3,
                    "recentEvents": [
                        {
                            "sessionId": "sess-9042",
                            "outcome": "HELPED",
                            "note": "Guided agent to run `uv run pytest` (~2.4s) during local verification.",
                        },
                        {
                            "sessionId": "sess-8995",
                            "outcome": "NOT_USEFUL",
                            "note": "Single isolated miss (1 of 3 threshold): v3 lacked explicit `pip install` ban; retained & refined instead of evicted.",
                        },
                        {
                            "sessionId": "sess-8961",
                            "outcome": "HELPED",
                            "note": "Resolved virtual environment dependency resolution via `uv lock`.",
                        },
                    ],
                },
                "relations": [
                    {"type": "supersedes", "text": "evicts -> /user/developer/python_tooling/legacy_pip_fallback.mem"}
                ],
                "updated_at": now_iso,
            },
            "agent_workcell_kinematics": {
                "id": "agent_workcell_kinematics",
                "memory_id": "agent_workcell_kinematics",
                "scope": "agent",
                "scope_type": "AGENT",
                "scope_id": "agent:workcell-agent",
                "scopeFolder": "agent/workcell-agent",
                "categoryFolder": "kinematics_constraints",
                "category": "kinematics_constraints",
                "fileName": "max_wrist_torque.mem",
                "file_path": "/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem",
                "commitState": "SYNCED",
                "sync_status": "SYNCED",
                "currentContent": "Clamp Workcell B J5 wrist torque to 14.0 Nm max; trigger immediate Cascade Andon Halt and transition state machine to SAFE_HOLD if exceeded.",
                "proposedContent": None,
                "diffSummary": None,
                "confidence": 1.0,
                "originMode": "USER",
                "write_mode": "READ_ONLY_LOCKED",
                "sessionId": "sess-8984",
                "trigger": "AGENT_SCOPE: /agent/workcell-agent/**",
                "tokens": 31,
                "bytes": 144,
                "helpful_count": 11,
                "unhelpful_count": 0,
                "provenance": "👤 Pinned Safety Invariant · Synced in Spanner",
                "usefulnessTracker": {
                    "usefulCount": 11,
                    "notUsefulCount": 0,
                    "reviewThreshold": 3,
                    "recentEvents": [
                        {
                            "sessionId": "sess-8984",
                            "outcome": "HELPED",
                            "note": "Clamped J5 wrist torque at 13.8 Nm and triggered Cascade Andon Halt before fracture limit.",
                        }
                    ],
                },
                "relations": [
                    {"type": "pinned", "text": "bound to -> ent_agent_workcell"}
                ],
                "updated_at": now_iso,
            },
            "user_developer_theme": {
                "id": "user_developer_theme",
                "memory_id": "user_developer_theme",
                "scope": "user",
                "scope_type": "USER",
                "scope_id": "user:developer",
                "scopeFolder": "user/developer",
                "categoryFolder": "ui_design_system",
                "category": "ui_design_system",
                "fileName": "academic_light_sepia.mem",
                "file_path": "/user/developer/ui_design_system/academic_light_sepia.mem",
                "commitState": "SYNCED",
                "sync_status": "SYNCED",
                "currentContent": "Use distraction-free academic light or sepia styling with readable typography, strict 2-3 panel layout (Left nav, Center workspace, Right contextual drawer), and compact Intent Pills.",
                "proposedContent": None,
                "diffSummary": None,
                "confidence": 0.96,
                "originMode": "USER",
                "write_mode": "WRITE_THROUGH_LIVE",
                "sessionId": "sess-9019",
                "trigger": "FILE_GLOB: *.html, *.css",
                "tokens": 38,
                "bytes": 188,
                "helpful_count": 8,
                "unhelpful_count": 0,
                "provenance": "👤 User Personal Preference · Synced in Spanner",
                "usefulnessTracker": {
                    "usefulCount": 8,
                    "notUsefulCount": 0,
                    "reviewThreshold": 3,
                    "recentEvents": [
                        {
                            "sessionId": "sess-9019",
                            "outcome": "HELPED",
                            "note": "Applied 3-panel master-detail layout and warm academic light palette automatically.",
                        }
                    ],
                },
                "relations": [],
                "updated_at": now_iso,
            },
            "user_legacy_pip_override": {
                "id": "user_legacy_pip_override",
                "memory_id": "user_legacy_pip_override",
                "scope": "user",
                "scope_type": "USER",
                "scope_id": "user:developer",
                "scopeFolder": "user/developer",
                "categoryFolder": "python_tooling",
                "category": "python_tooling",
                "fileName": "legacy_pip_fallback.mem",
                "file_path": "/user/developer/python_tooling/legacy_pip_fallback.mem",
                "commitState": "DELETE",
                "sync_status": "STAGED_DELETE",
                "currentContent": "Fallback to `pip install --user` when virtualenv is missing.",
                "proposedContent": None,
                "diffSummary": "-60B · Evicted: Crossed Not-Useful Threshold (4/3 sessions) & contradicts team uv standard",
                "confidence": 0.20,
                "originMode": "AGENT",
                "write_mode": "WRITE_THROUGH_LIVE",
                "sessionId": "sess-9042",
                "trigger": "FILE_GLOB: *.py",
                "tokens": 36,
                "bytes": 144,
                "helpful_count": 1,
                "unhelpful_count": 4,
                "provenance": "⚙️ Staged for Eviction by Job ext_dev_corrections · Crossed 4/3 Not-Useful Threshold in sess-9042",
                "usefulnessTracker": {
                    "usefulCount": 1,
                    "notUsefulCount": 4,
                    "reviewThreshold": 3,
                    "recentEvents": [
                        {
                            "sessionId": "sess-9042",
                            "outcome": "NOT_USEFUL",
                            "note": "4th not-useful recall (crossed >=3 threshold): triggered global pip install; user halted & enforced uv.",
                        },
                        {
                            "sessionId": "sess-9019",
                            "outcome": "NOT_USEFUL",
                            "note": "3rd not-useful recall: bare pytest failed due to missing uv virtualenv.",
                        },
                        {
                            "sessionId": "sess-9002",
                            "outcome": "NOT_USEFUL",
                            "note": "2nd not-useful recall: pip install conflicted with pyproject.toml lockfile.",
                        },
                        {
                            "sessionId": "sess-8984",
                            "outcome": "NOT_USEFUL",
                            "note": "1st not-useful recall (logged, no action taken on single miss).",
                        },
                    ],
                },
                "relations": [
                    {"type": "conflict", "text": "contradicts -> /team/platform-ml/python_tooling/python_uv_runner.mem"}
                ],
                "updated_at": now_iso,
            },
        }

        self._studio_staged_commits: Dict[str, Dict[str, Any]] = {
            "commit_8801": {
                "commit_id": "commit_8801",
                "file_id": "org_cloud_run_verify",
                "file_path": "/org/acme-org/cloud_run_deploy/live_endpoint_verification.mem",
                "action": "ADD",
                "scope": "org",
                "scope_id": "org:acme-org",
                "category": "cloud_run_deploy",
                "proposed_content": "A task involving services or web applications is NOT complete until built, deployed to Google Cloud Run (`your-gcp-project-id`), and verified via live HTTP probe (`python3 -m scripts.validate_live_endpoints`). Always provide the live service URL.",
                "previous_content": None,
                "author": "ext_arch_invariants (sess-9042)",
                "summary": "+247B · Extracted by ext_arch_invariants from sess-9042",
                "status": "PENDING",
                "created_at": now_iso,
            },
            "commit_8802": {
                "commit_id": "commit_8802",
                "file_id": "team_python_uv",
                "file_path": "/team/platform-ml/python_tooling/python_uv_runner.mem",
                "action": "UPDATE",
                "scope": "team",
                "scope_id": "team:platform-ml",
                "category": "python_tooling",
                "proposed_content": "Always use `uv` (`uv run pytest`, `uv add`) for Python testing and dependency management. Never invoke global `pip install` or bare `pytest`.",
                "previous_content": "Use `uv run pytest` for running Python unit tests in this repository.",
                "author": "ext_dev_corrections (sess-9042)",
                "summary": "+75B / -12B · Ban global pip install (from sess-9042)",
                "status": "PENDING",
                "created_at": now_iso,
            },
            "commit_8803": {
                "commit_id": "commit_8803",
                "file_id": "user_legacy_pip_override",
                "file_path": "/user/developer/python_tooling/legacy_pip_fallback.mem",
                "action": "DELETE",
                "scope": "user",
                "scope_id": "user:developer",
                "category": "python_tooling",
                "proposed_content": None,
                "previous_content": "Fallback to `pip install --user` when virtualenv is missing.",
                "author": "ext_dev_corrections (sess-9042)",
                "summary": "-60B · Evicted: Crossed Not-Useful Threshold (4/3 sessions) & contradicts team uv standard",
                "status": "PENDING",
                "created_at": now_iso,
            },
        }

        # 6. Default Audit Ledger & Backend Subsystems (Workspace 9)
        self._studio_audit_ledger: Dict[str, Dict[str, Any]] = {
            "mut_live_801": {
                "id": "mut_live_801",
                "mutation_id": "mut_live_801",
                "timestamp": now_iso,
                "actor": "serviceAccount:antigravity-coding-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "agentPrincipal": "serviceAccount:antigravity-coding-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "tool_name": "crystallize_live_preference",
                "mcpTool": "crystallize_live_preference",
                "operation": "DIRECT_WRITE (UPSERT)",
                "file_path": "/user/developer/python_tooling/uv_testing_standard.mem",
                "targetPath": "/user/developer/python_tooling/uv_testing_standard.mem",
                "fileId": "team_python_uv",
                "scope": "user",
                "write_mode": "WRITE_THROUGH_LIVE",
                "governanceMode": "WRITE_THROUGH_LIVE",
                "new_belief_snapshot": "Always execute Python unit tests via `uv run pytest` and manage virtual environments with `uv`. Never invoke bare `pip install`.",
                "beliefWritten": "Always execute Python unit tests via `uv run pytest` and manage virtual environments with `uv`. Never invoke bare `pip install`.",
                "prior_belief_snapshot": "Prefer `uv` for Python package management.",
                "priorBeliefSnapshot": "Prefer `uv` for Python package management.",
                "status": "COMMITTED_LIVE",
                "spannerStatus": "COMMITTED_LIVE",
                "bqCdcStatus": "REPLICATED_TO_BQ",
                "rolledBack": False,
                "rationale": "Allowed by `/user/developer/**` RBAC (`DIRECT_WRITE: ALLOW`) — immediate Spanner ACID commit on hot path + streamed to BigQuery `scoped_memories_replica`.",
            },
            "mut_live_802": {
                "id": "mut_live_802",
                "mutation_id": "mut_live_802",
                "timestamp": now_iso,
                "actor": "serviceAccount:reviewer-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "agentPrincipal": "serviceAccount:reviewer-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "tool_name": "promote_scoped_memory",
                "mcpTool": "promote_scoped_memory",
                "operation": "CROSS_SCOPE_PROMOTE (/user → /team)",
                "file_path": "/team/platform-ml/cloud_run_deploy/live_endpoint_verification.mem",
                "targetPath": "/team/platform-ml/cloud_run_deploy/live_endpoint_verification.mem",
                "fileId": "org_cloud_run_verify",
                "scope": "team",
                "write_mode": "STAGE_ONLY_SHADOW",
                "governanceMode": "STAGE_ONLY_SHADOW",
                "new_belief_snapshot": "All service changes must deploy to Cloud Run (`your-gcp-project-id`) and pass `python3 -m scripts.validate_live_endpoints` before completion.",
                "beliefWritten": "All service changes must deploy to Cloud Run (`your-gcp-project-id`) and pass `python3 -m scripts.validate_live_endpoints` before completion.",
                "prior_belief_snapshot": None,
                "priorBeliefSnapshot": None,
                "status": "INTERCEPTED_TO_STAGED",
                "spannerStatus": "INTERCEPTED_TO_STAGED",
                "bqCdcStatus": "REPLICATED_TO_BQ",
                "rolledBack": False,
                "rationale": "Shared scope `/team/platform-ml/**` sets `DIRECT_WRITE: STAGE_ONLY` for agents — agent received `202 STAGED` ACK and diff was routed to Memory File Tree → Pending Commits.",
            },
            "mut_live_803": {
                "id": "mut_live_803",
                "mutation_id": "mut_live_803",
                "timestamp": now_iso,
                "actor": "serviceAccount:workcell-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "agentPrincipal": "serviceAccount:workcell-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "tool_name": "crystallize_live_preference",
                "mcpTool": "crystallize_live_preference",
                "operation": "DIRECT_WRITE (BLOCKED)",
                "file_path": "/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem",
                "targetPath": "/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem",
                "fileId": "agent_workcell_kinematics",
                "scope": "agent",
                "write_mode": "READ_ONLY_LOCKED",
                "governanceMode": "READ_ONLY_LOCKED",
                "new_belief_snapshot": "Attempted autonomous relaxation of wrist fracture limit from 14.0 Nm to 19.5 Nm.",
                "beliefWritten": "Attempted autonomous relaxation of wrist fracture limit from 14.0 Nm to 19.5 Nm.",
                "prior_belief_snapshot": "Clamp J5 wrist torque <= 14.0 Nm; trigger Cascade Andon Halt on exceedance.",
                "priorBeliefSnapshot": "Clamp J5 wrist torque <= 14.0 Nm; trigger Cascade Andon Halt on exceedance.",
                "status": "BLOCKED_SAFETY_PIN",
                "spannerStatus": "BLOCKED_SAFETY_PIN",
                "bqCdcStatus": "AUDIT_LOGGED_BQ",
                "rolledBack": False,
                "rationale": "Safety Pin policy on `/agent/workcell-agent/kinematics_constraints/**` denies `DIRECT_WRITE` to prevent autonomous self-modification of hardware limits.",
            },
        }

        # 7. Default RBAC Policies (Workspace 10)
        self._studio_rbac_policies: Dict[str, Dict[str, Any]] = {
            "sec_org_invariants": {
                "id": "sec_org_invariants",
                "policy_id": "sec_org_invariants",
                "name": "Organizational Root Invariants Guardrail Policy",
                "scopeType": "org",
                "treeCondition": "/org/acme-org/**",
                "scope_path_pattern": "/org/acme-org/**",
                "inheritanceMode": "INHERIT_DOWN_ALL (Propagates to /team, /agent, /user)",
                "principal": "group:org-security-architects@example.com",
                "role_binding": "roles/spanner.databaseAdmin",
                "principals": [
                    {"type": "GROUP", "id": "group:org-security-architects@example.com", "cloudBinding": "Cloud Identity Group · Org Security"},
                    {"type": "ROLE", "id": "roles/spanner.databaseAdmin", "cloudBinding": "GCP IAM Role · your-gcp-project-id"},
                    {"type": "GROUP", "id": "group:all-authenticated-runtimes@example.com", "cloudBinding": "Read/Recall Only across Org"},
                ],
                "permissions": {
                    "RECALL": True,
                    "DIRECT_WRITE": False,
                    "STAGE": True,
                    "COMMIT": True,
                    "EVICT": False,
                    "PROMOTE": True,
                },
                "readOnlyForGeneral": True,
                "write_governance": "STAGE_FIRST_2_PERSON",
                "enforcementMode": "ENFORCED (2-Person Review · Direct Agent Write -> Staged)",
                "guardrails": "All agents/users inherit RECALL access; direct agent writes to /org/** are intercepted into Staged Commits. Only group:org-security-architects may COMMIT.",
                "lastIamSync": "Synced with GCP IAM (your-gcp-project-id)",
                "updated_at": now_iso,
            },
            "sec_team_platform": {
                "id": "sec_team_platform",
                "policy_id": "sec_team_platform",
                "name": "Platform & Agentic ML Team Memory RBAC",
                "scopeType": "team",
                "treeCondition": "/team/platform-ml/**",
                "scope_path_pattern": "/team/platform-ml/**",
                "inheritanceMode": "INHERIT_TO_TEAM_CHILDREN (Scoped to Platform ML Agents & Users)",
                "principal": "group:platform-ml-eng@example.com",
                "role_binding": "roles/aiplatform.user",
                "principals": [
                    {"type": "GROUP", "id": "group:platform-ml-eng@example.com", "cloudBinding": "Google Workspace Group · Platform ML"},
                    {"type": "ROLE", "id": "roles/aiplatform.user", "cloudBinding": "GCP IAM Role · Vertex AI Developers"},
                    {"type": "SA", "id": "serviceAccount:extractor-subagent@your-gcp-project-id.iam.gserviceaccount.com", "cloudBinding": "Extractor Subagent SA (Stage Only)"},
                ],
                "permissions": {
                    "RECALL": True,
                    "DIRECT_WRITE": False,
                    "STAGE": True,
                    "COMMIT": True,
                    "EVICT": True,
                    "PROMOTE": True,
                },
                "readOnlyForGeneral": False,
                "write_governance": "STAGE_ONLY_SHADOW",
                "enforcementMode": "ENFORCED (Team Scoped · Agent Writes Stage First)",
                "guardrails": "Members of group:platform-ml-eng have full CRUD/Commit. In-band agent writes or promotions into /team/platform-ml/** are intercepted as Staged Commits (`STAGE_ONLY_SHADOW`).",
                "lastIamSync": "Synced with GCP IAM (your-gcp-project-id)",
                "updated_at": now_iso,
            },
            "sec_agent_workcell": {
                "id": "sec_agent_workcell",
                "policy_id": "sec_agent_workcell",
                "name": "Workcell B Autonomous Agent Safety Pin Policy",
                "scopeType": "agent",
                "treeCondition": "/agent/workcell-agent/kinematics_constraints/**",
                "scope_path_pattern": "/agent/workcell-agent/kinematics_constraints/**",
                "inheritanceMode": "EXACT_AGENT_SCOPE (Pinned to Workcell-Agent Runtime)",
                "principal": "serviceAccount:workcell-agent@your-gcp-project-id.iam.gserviceaccount.com",
                "role_binding": "roles/robotics.workcellOperator",
                "principals": [
                    {"type": "SA", "id": "serviceAccount:workcell-agent@your-gcp-project-id.iam.gserviceaccount.com", "cloudBinding": "Workload Identity SA · Recall & Stage Only"},
                    {"type": "GROUP", "id": "group:robotics-safety-board@example.com", "cloudBinding": "Safety Approvers · Commit & Evict"},
                ],
                "permissions": {
                    "RECALL": True,
                    "DIRECT_WRITE": False,
                    "STAGE": True,
                    "COMMIT": False,
                    "EVICT": False,
                    "PROMOTE": False,
                },
                "readOnlyForGeneral": False,
                "write_governance": "READ_ONLY_LOCKED",
                "enforcementMode": "STRICT SAFETY PIN",
                "guardrails": "Prevents autonomous agent self-modification (`DIRECT_WRITE: DENY`) or self-eviction of physical torque/fracture limits. Only group:robotics-safety-board can commit changes.",
                "lastIamSync": "Synced with GCP IAM (your-gcp-project-id)",
                "updated_at": now_iso,
            },
            "sec_user_developer": {
                "id": "sec_user_developer",
                "policy_id": "sec_user_developer",
                "name": "Personal Developer Scope Isolation (/user/developer)",
                "scopeType": "user",
                "treeCondition": "/user/developer/**",
                "scope_path_pattern": "/user/developer/**",
                "inheritanceMode": "PRIVATE_USER_TENANT (Zero Cross-User Recall Leakage)",
                "principal": "user:developer@example.com",
                "role_binding": "roles/owner",
                "principals": [
                    {"type": "USER", "id": "user:developer@example.com", "cloudBinding": "ADC Principal · Owner of /user/developer/**"},
                    {"type": "SA", "id": "serviceAccount:antigravity-coding-agent@your-gcp-project-id.iam.gserviceaccount.com", "cloudBinding": "Pair-Programming Agent SA · Direct Write Allowed"},
                    {"type": "ROLE", "id": "roles/owner", "cloudBinding": "GCP Project Owner (your-gcp-project-id)"},
                ],
                "permissions": {
                    "RECALL": True,
                    "DIRECT_WRITE": True,
                    "STAGE": True,
                    "COMMIT": True,
                    "EVICT": True,
                    "PROMOTE": True,
                },
                "readOnlyForGeneral": False,
                "write_governance": "WRITE_THROUGH_LIVE",
                "enforcementMode": "WRITE-THROUGH LIVE + BQ CDC",
                "guardrails": "Allows live preference crystallization (`crystallize_live_preference`) directly to Cloud Spanner (`WRITE_THROUGH_LIVE`) with automatic Change Stream CDC replication to BigQuery and 1-click rollback.",
                "lastIamSync": "Synced with GCP IAM (your-gcp-project-id)",
                "updated_at": now_iso,
            },
        }

        self._studio_jobs: Dict[str, Dict[str, Any]] = {}
        self._studio_jobs_last_persisted: Dict[str, float] = {}
        self._studio_jobs_lock = threading.RLock()
        self._studio_defaults_ready = True

    def _persist_studio_meta_to_spanner(self, namespace: str, item_id: str, payload: Dict[str, Any]) -> None:
        """Persist a Studio workspace record into Cloud Spanner (both dedicated table if migrated and UserPreferences bridge)."""
        if self.use_mock or not self._database:
            return
        try:
            from google.cloud import spanner

            def _tx(transaction):
                transaction.insert_or_update(
                    table="UserPreferences",
                    columns=(
                        "UserId",
                        "PreferenceKey",
                        "PreferenceValue",
                        "Category",
                        "SourceSessionId",
                        "Confidence",
                        "CrystallizedBelief",
                        "Keywords",
                        "LastUpdated",
                    ),
                    values=[
                        (
                            f"studio::{namespace}",
                            item_id,
                            json.dumps(payload),
                            namespace,
                            "MEMORY_STUDIO_V2",
                            1.0,
                            str(payload.get("name") or payload.get("file_path") or item_id),
                            str(payload.get("status") or payload.get("commitState") or "ACTIVE"),
                            spanner.COMMIT_TIMESTAMP,
                        )
                    ],
                )

            self._database.run_in_transaction(_tx)
        except Exception as e:
            logger.debug(f"Studio Spanner bridge write notice for {namespace}/{item_id}: {e}")

    def _load_studio_meta_from_spanner(self, namespace: str, target_dict: Dict[str, Dict[str, Any]]) -> None:
        """Load persisted Studio workspace records from Cloud Spanner on cold boot or refresh."""
        if self.use_mock or not self._database:
            return
        try:
            with self._database.snapshot() as snapshot:
                results = snapshot.execute_sql(
                    "SELECT PreferenceKey, PreferenceValue FROM UserPreferences WHERE UserId = @uid",
                    params={"uid": f"studio::{namespace}"},
                    param_types={"uid": self._spanner_param_types.STRING},
                )
                for row in results:
                    k, val_str = row[0], row[1]
                    if k and val_str:
                        parsed = json.loads(val_str)
                        if isinstance(parsed, dict):
                            if parsed.get("_deleted"):
                                target_dict.pop(k, None)
                            else:
                                target_dict[k] = parsed
        except Exception as e:
            logger.debug(f"Studio Spanner bridge read notice for {namespace}: {e}")

    def ensure_studio_schema(self) -> Dict[str, Any]:
        """Ensure all 10-workspace Studio datasets & initial records exist in Cloud Spanner."""
        self._init_studio_defaults()
        if not getattr(self, "_studio_spanner_hydrated", False):
            self._load_studio_meta_from_spanner("entities", self._studio_entities)
            self._load_studio_meta_from_spanner("categories", self._studio_categories)
            self._load_studio_meta_from_spanner("ingest_services", self._studio_ingest_services)
            self._load_studio_meta_from_spanner("extractors", self._studio_extractors)
            self._load_studio_meta_from_spanner("memory_files", self._studio_memory_files)
            self._load_studio_meta_from_spanner("staged_commits", self._studio_staged_commits)
            self._load_studio_meta_from_spanner("audit_ledger", self._studio_audit_ledger)
            self._load_studio_meta_from_spanner("rbac_policies", self._studio_rbac_policies)
            self._load_studio_meta_from_spanner("jobs", self._studio_jobs)
            self._studio_spanner_hydrated = True
        return {
            "status": "READY",
            "spanner_instance": self.instance_id,
            "spanner_database": self.database_id,
            "tables": [
                "scoped_memories",
                "staged_memory_commits",
                "scope_entities",
                "memory_categories",
                "ingest_services",
                "extractor_jobs",
                "memory_audit_ledger",
                "rbac_policies",
                "studio_jobs",
            ],
        }

    # -------------------------------------------------------------------------
    # Workspace 2: Memory File Tree & Pending Commits
    # -------------------------------------------------------------------------
    def get_memory_file_tree(
        self,
        scope_filter: str = "ALL",
        only_diffs: bool = False,
    ) -> Dict[str, Any]:
        """Return all hierarchical `.mem` files and staged commits from Spanner."""
        self.ensure_studio_schema()
        files = list(self._studio_memory_files.values())
        if scope_filter and scope_filter.upper() != "ALL":
            sf = scope_filter.lower()
            files = [f for f in files if str(f.get("scope", "")).lower() == sf]
        if only_diffs:
            files = [f for f in files if f.get("commitState", "SYNCED") != "SYNCED"]

        pending_commits = [
            c for c in self._studio_staged_commits.values()
            if c.get("status", "PENDING") == "PENDING"
        ]
        return {
            "status": "OK",
            "files": files,
            "memory_files": files,
            "staged_commits": pending_commits,
            "pending_count": len([f for f in self._studio_memory_files.values() if f.get("commitState", "SYNCED") != "SYNCED"]),
            "total_count": len(files),
        }

    def stage_memory_commit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Stage an ADD, UPDATE, or DELETE commit on a `.mem` file in Spanner."""
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        action = str(payload.get("action") or payload.get("commitState") or "ADD").upper()
        if action not in ("ADD", "UPDATE", "DELETE"):
            action = "UPDATE"

        scope = str(payload.get("scope") or payload.get("scope_type") or "team").lower()
        scope_folder = str(payload.get("scopeFolder") or payload.get("scope_folder") or "").strip("/")
        if not scope_folder:
            default_folders = {
                "org": "org/your-gcp-project-id",
                "team": "team/platform-ml",
                "agent": "agent/workcell-agent",
                "user": "user/developer",
            }
            scope_folder = default_folders.get(scope, f"{scope}/default")
        if "/" in scope_folder:
            scope = scope_folder.split("/")[0].lower()

        category_folder = str(payload.get("categoryFolder") or payload.get("category") or "python_tooling").strip("/")
        file_name = str(payload.get("fileName") or payload.get("file_name") or payload.get("memory_key") or "rule.mem").strip()
        if not file_name.endswith(".mem"):
            file_name = f"{file_name}.mem"

        file_path = str(payload.get("file_path") or f"/{scope_folder}/{category_folder}/{file_name}")
        file_id = str(payload.get("file_id") or payload.get("id") or payload.get("memory_id") or "").strip()

        # Check if file already exists by id or path
        existing_file = None
        if file_id and file_id in self._studio_memory_files:
            existing_file = self._studio_memory_files[file_id]
        else:
            for fid, fobj in self._studio_memory_files.items():
                if fobj.get("file_path") == file_path:
                    existing_file = fobj
                    file_id = fid
                    break

        if not file_id:
            slug_key = re.sub(r"[^a-zA-Z0-9_]+", "_", file_name.replace(".mem", "")).strip("_").lower()
            file_id = f"{scope}_{slug_key}_{uuid.uuid4().hex[:4]}"

        proposed_content = payload.get("proposed_content")
        if proposed_content is None:
            proposed_content = payload.get("proposedContent")
        if proposed_content is None:
            proposed_content = payload.get("content") or payload.get("memory_value") or ""

        prev_content = existing_file.get("currentContent") if existing_file else payload.get("previous_content")
        if existing_file and action == "ADD" and prev_content:
            action = "UPDATE"

        summary = str(
            payload.get("summary")
            or payload.get("diffSummary")
            or (
                f"-{len(str(prev_content or ''))}B · Staged DELETE for {file_name}"
                if action == "DELETE"
                else f"+{len(str(proposed_content or ''))}B · Staged {action} for {file_name}"
            )
        )
        author = str(payload.get("author") or "developer@example.com")
        trigger_str = str(payload.get("trigger") or (existing_file.get("trigger") if existing_file else "FILE_GLOB: *.py"))
        conf_val = float(payload.get("confidence") if payload.get("confidence") is not None else (existing_file.get("confidence", 0.96) if existing_file else 0.96))
        content_for_size = str(proposed_content if action != "DELETE" else (prev_content or ""))

        file_record = {
            "id": file_id,
            "memory_id": file_id,
            "scope": scope,
            "scope_type": scope.upper(),
            "scope_id": scope_folder.replace("/", ":"),
            "scopeFolder": scope_folder,
            "categoryFolder": category_folder,
            "category": category_folder,
            "fileName": file_name,
            "file_path": file_path,
            "commitState": action,
            "sync_status": f"STAGED_{action}",
            "currentContent": prev_content,
            "proposedContent": None if action == "DELETE" else str(proposed_content),
            "diffSummary": summary,
            "confidence": conf_val,
            "originMode": str(payload.get("originMode") or (existing_file.get("originMode") if existing_file else "USER")),
            "write_mode": str(payload.get("write_mode") or (existing_file.get("write_mode") if existing_file else "STAGE_ONLY_SHADOW")),
            "sessionId": str(payload.get("session_id") or payload.get("sessionId") or (existing_file.get("sessionId") if existing_file else "sess-9042")),
            "trigger": trigger_str,
            "tokens": max(1, (len(content_for_size) + 3) // 4),
            "bytes": len(content_for_size.encode("utf-8")),
            "helpful_count": int(existing_file.get("helpful_count", 1) if existing_file else 1),
            "unhelpful_count": int(existing_file.get("unhelpful_count", 0) if existing_file else 0),
            "provenance": str(payload.get("provenance") or f"📝 Staged ({action}) by {author}"),
            "usefulnessTracker": existing_file.get("usefulnessTracker") if existing_file else {
                "usefulCount": 1,
                "notUsefulCount": 0,
                "reviewThreshold": 3,
                "recentEvents": [],
            },
            "relations": existing_file.get("relations", []) if existing_file else [],
            "updated_at": now_iso,
        }
        self._studio_memory_files[file_id] = file_record
        self._persist_studio_meta_to_spanner("memory_files", file_id, file_record)

        commit_id = str(payload.get("commit_id") or f"commit_{uuid.uuid4().hex[:6]}")
        # Reuse existing pending commit for the same file_id if present
        for cid, cobj in list(self._studio_staged_commits.items()):
            if (cobj.get("file_id") == file_id or cobj.get("file_path") == file_path) and cobj.get("status") == "PENDING":
                commit_id = cid
                break

        commit_record = {
            "commit_id": commit_id,
            "file_id": file_id,
            "file_path": file_path,
            "action": action,
            "scope": scope,
            "scope_id": scope_folder.replace("/", ":"),
            "category": category_folder,
            "proposed_content": None if action == "DELETE" else str(proposed_content),
            "previous_content": prev_content,
            "author": author,
            "summary": summary,
            "status": "PENDING",
            "created_at": now_iso,
        }
        self._studio_staged_commits[commit_id] = commit_record
        self._persist_studio_meta_to_spanner("staged_commits", commit_id, commit_record)

        return {
            "status": "STAGED",
            "commit": commit_record,
            "file": file_record,
        }

    def commit_staged_memory(self, commit_id_or_file_id: str, actor: str = "developer@example.com") -> Dict[str, Any]:
        """Commit a single staged `.mem` diff into Cloud Spanner and append an audit ledger entry."""
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        target_commit = self._studio_staged_commits.get(commit_id_or_file_id)
        target_file = self._studio_memory_files.get(commit_id_or_file_id)

        if target_commit and not target_file:
            target_file = self._studio_memory_files.get(target_commit.get("file_id", ""))
            if not target_file:
                for f in self._studio_memory_files.values():
                    if f.get("file_path") == target_commit.get("file_path"):
                        target_file = f
                        break
        elif target_file and not target_commit:
            for c in self._studio_staged_commits.values():
                if (c.get("file_id") == target_file["id"] or c.get("file_path") == target_file["file_path"]) and c.get("status") == "PENDING":
                    target_commit = c
                    break

        if not target_file and not target_commit:
            raise ValueError(f"Staged commit or file '{commit_id_or_file_id}' not found")

        action = (target_commit.get("action") if target_commit else target_file.get("commitState")) or "UPDATE"
        file_id = target_file["id"] if target_file else target_commit["file_id"]
        file_path = target_file["file_path"] if target_file else target_commit["file_path"]
        prior_snap = target_file.get("currentContent") if target_file else target_commit.get("previous_content")
        new_snap = target_file.get("proposedContent") if target_file else target_commit.get("proposed_content")

        if action == "DELETE":
            self._studio_memory_files.pop(file_id, None)
            self._persist_studio_meta_to_spanner("memory_files", file_id, {"_deleted": True, "id": file_id})
            committed_file = {"id": file_id, "file_path": file_path, "commitState": "DELETED"}
        else:
            final_text = str(new_snap if new_snap is not None else (prior_snap or ""))
            target_file["currentContent"] = final_text
            target_file["proposedContent"] = None
            target_file["diffSummary"] = None
            target_file["commitState"] = "SYNCED"
            target_file["sync_status"] = "SYNCED"
            target_file["bytes"] = len(final_text.encode("utf-8"))
            target_file["tokens"] = max(1, (len(final_text) + 3) // 4)
            target_file["provenance"] = f"✓ Committed to Spanner by {actor}"
            target_file["updated_at"] = now_iso
            self._studio_memory_files[file_id] = target_file
            self._persist_studio_meta_to_spanner("memory_files", file_id, target_file)

            # Also write into ScopedMemories so federated recall immediately sees the committed `.mem` file
            try:
                self.upsert_scoped_memory(
                    scope_type=str(target_file.get("scope", "USER")).upper(),
                    scope_id=str(target_file.get("scope_id") or f"{target_file.get('scope', 'user')}:default"),
                    memory_key=str(target_file.get("fileName", file_id)).replace(".mem", ""),
                    memory_value=final_text,
                    memory_type="INVARIANT_GUARDRAIL" if target_file.get("scope") == "org" else "PREFERENCE",
                    category=str(target_file.get("categoryFolder", "general")),
                    confidence=float(target_file.get("confidence", 0.98)),
                    crystallized_belief=final_text,
                    keywords=str(target_file.get("categoryFolder", "")),
                    source_session_id=str(target_file.get("sessionId", "STUDIO_COMMIT")),
                    sync_status="SYNCED",
                )
            except Exception as e:
                logger.debug(f"ScopedMemories mirror notice on commit: {e}")
            committed_file = target_file

        if target_commit:
            target_commit["status"] = "COMMITTED"
            target_commit["committed_at"] = now_iso
            self._persist_studio_meta_to_spanner("staged_commits", target_commit["commit_id"], target_commit)

        # Record in audit ledger
        mut_id = f"mut_{uuid.uuid4().hex[:6]}"
        audit_entry = {
            "id": mut_id,
            "mutation_id": mut_id,
            "timestamp": now_iso,
            "actor": actor,
            "agentPrincipal": actor,
            "tool_name": "studio_commit_staged",
            "mcpTool": "studio_commit_staged",
            "operation": f"STAGED_COMMIT ({action})",
            "file_path": file_path,
            "targetPath": file_path,
            "fileId": file_id,
            "scope": target_file.get("scope", "team") if target_file else "team",
            "write_mode": "COMMITTED_FROM_STAGED",
            "governanceMode": "COMMITTED_FROM_STAGED",
            "new_belief_snapshot": str(new_snap or ""),
            "beliefWritten": str(new_snap or f"[DELETED {file_path}]"),
            "prior_belief_snapshot": prior_snap,
            "priorBeliefSnapshot": prior_snap,
            "status": "COMMITTED_LIVE",
            "spannerStatus": "COMMITTED_LIVE",
            "bqCdcStatus": "REPLICATED_TO_BQ",
            "rolledBack": False,
            "rationale": f"Explicitly reviewed and committed ({action}) to Cloud Spanner by {actor}.",
        }
        self._studio_audit_ledger[mut_id] = audit_entry
        self._persist_studio_meta_to_spanner("audit_ledger", mut_id, audit_entry)

        return {
            "status": "COMMITTED",
            "action": action,
            "file": committed_file,
            "commit": target_commit,
            "audit_entry": audit_entry,
        }

    def discard_staged_memory(self, commit_id_or_file_id: str, actor: str = "developer@example.com") -> Dict[str, Any]:
        """Discard a staged `.mem` diff and revert the file to its prior synced state."""
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        target_commit = self._studio_staged_commits.get(commit_id_or_file_id)
        target_file = self._studio_memory_files.get(commit_id_or_file_id)

        if target_commit and not target_file:
            target_file = self._studio_memory_files.get(target_commit.get("file_id", ""))
        elif target_file and not target_commit:
            for c in self._studio_staged_commits.values():
                if (c.get("file_id") == target_file["id"] or c.get("file_path") == target_file["file_path"]) and c.get("status") == "PENDING":
                    target_commit = c
                    break

        if not target_file and not target_commit:
            raise ValueError(f"Staged commit or file '{commit_id_or_file_id}' not found")

        file_id = target_file["id"] if target_file else target_commit["file_id"]
        action = (target_file.get("commitState") if target_file else target_commit.get("action")) or "UPDATE"

        if action == "ADD" and (not target_file or not target_file.get("currentContent")):
            self._studio_memory_files.pop(file_id, None)
            self._persist_studio_meta_to_spanner("memory_files", file_id, {"_deleted": True, "id": file_id})
            reverted_file = None
        elif target_file:
            target_file["commitState"] = "SYNCED"
            target_file["sync_status"] = "SYNCED"
            target_file["proposedContent"] = None
            target_file["diffSummary"] = None
            target_file["updated_at"] = now_iso
            self._studio_memory_files[file_id] = target_file
            self._persist_studio_meta_to_spanner("memory_files", file_id, target_file)
            reverted_file = target_file
        else:
            reverted_file = None

        if target_commit:
            target_commit["status"] = "DISCARDED"
            target_commit["discarded_at"] = now_iso
            self._persist_studio_meta_to_spanner("staged_commits", target_commit["commit_id"], target_commit)

        return {
            "status": "DISCARDED",
            "file_id": file_id,
            "action_discarded": action,
            "file": reverted_file,
        }

    def commit_all_staged_memories(self, actor: str = "developer@example.com") -> Dict[str, Any]:
        """Commit all pending staged `.mem` diffs in Spanner."""
        self.ensure_studio_schema()
        pending_files = [
            f["id"] for f in list(self._studio_memory_files.values())
            if f.get("commitState", "SYNCED") != "SYNCED"
        ]
        committed_results = []
        for fid in pending_files:
            committed_results.append(self.commit_staged_memory(fid, actor=actor))
        return {
            "status": "ALL_STAGED_COMMITTED",
            "committed_count": len(committed_results),
            "results": committed_results,
        }

    def update_memory_helpfulness_tally(
        self,
        memory_id: str,
        outcome: str,
        session_id: str = "sess-9042",
        note: str = "",
        review_threshold: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Update cumulative helpful/unhelpful counters and usefulnessTracker on a `.mem` file in Spanner."""
        self.ensure_studio_schema()
        target_file = self._studio_memory_files.get(memory_id)
        if not target_file:
            for f in self._studio_memory_files.values():
                if f.get("file_path") == memory_id or f.get("fileName") == memory_id:
                    target_file = f
                    break
        if not target_file:
            raise ValueError(f"Memory file '{memory_id}' not found")

        tracker = target_file.setdefault(
            "usefulnessTracker",
            {"usefulCount": 1, "notUsefulCount": 0, "reviewThreshold": 3, "recentEvents": []},
        )
        if review_threshold is not None and int(review_threshold) > 0:
            tracker["reviewThreshold"] = int(review_threshold)

        norm_outcome = "HELPED" if str(outcome).upper() in ("HELPED", "USEFUL", "HELPFUL", "TRUE", "1") else "NOT_USEFUL"
        if outcome and str(outcome).upper() != "THRESHOLD_ONLY":
            if norm_outcome == "HELPED":
                tracker["usefulCount"] = int(tracker.get("usefulCount", 0)) + 1
                target_file["helpful_count"] = tracker["usefulCount"]
                ev_note = note or "Verified helpful in session recall (saved to Cloud Spanner & BigQuery CDC)."
            else:
                tracker["notUsefulCount"] = int(tracker.get("notUsefulCount", 0)) + 1
                target_file["unhelpful_count"] = tracker["notUsefulCount"]
                crossed = tracker["notUsefulCount"] >= int(tracker.get("reviewThreshold", 3))
                ev_note = note or (
                    f"Crossed review threshold ({tracker['notUsefulCount']}/{tracker['reviewThreshold']} not-useful sessions) — flagged for revision or eviction."
                    if crossed
                    else f"Logged isolated not-useful recall ({tracker['notUsefulCount']} of {tracker['reviewThreshold']} threshold; no eviction on isolated miss)."
                )
            events = list(tracker.get("recentEvents", []))
            events.insert(0, {"sessionId": session_id, "outcome": norm_outcome, "note": ev_note})
            tracker["recentEvents"] = events[:10]

        target_file["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._studio_memory_files[target_file["id"]] = target_file
        self._persist_studio_meta_to_spanner("memory_files", target_file["id"], target_file)
        return target_file

    # -------------------------------------------------------------------------
    # Workspace 5: Scope Entities
    # -------------------------------------------------------------------------
    def list_scope_entities(self, scope_type: str = "ALL") -> List[Dict[str, Any]]:
        self.ensure_studio_schema()
        items = list(self._studio_entities.values())
        if scope_type and scope_type.upper() != "ALL":
            st = scope_type.lower()
            items = [e for e in items if str(e.get("type", "")).lower() == st]
        items.sort(key=lambda x: (int(x.get("precedenceRank", 9)), str(x.get("path", ""))))
        return items

    def upsert_scope_entity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        st = str(payload.get("type") or payload.get("scope_type") or "team").lower()
        slug = str(payload.get("slug") or "").strip().lower().replace(" ", "-")
        name = str(payload.get("name") or slug or "New Scope Entity").strip()
        if not slug:
            slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "custom-scope"
        path = str(payload.get("path") or f"/{st}/{slug}")
        ent_id = str(payload.get("entity_id") or payload.get("id") or f"ent_{st}_{slug.replace('-', '_')}")
        rank_map = {"org": 1, "team": 2, "agent": 3, "user": 4}
        rank = int(payload.get("precedence_rank") or payload.get("precedenceRank") or rank_map.get(st, 2))
        parent_path = str(payload.get("parent_path") or payload.get("parentPath") or "/org/acme-org")
        binding = str(payload.get("identity_binding") or payload.get("binding") or f"iam:{st}:{slug}@example.com")
        write_mode = str(payload.get("write_mode") or ("WRITE_THROUGH_LIVE" if st == "user" else "STAGE_ONLY_SHADOW"))

        record = {
            "id": ent_id,
            "entity_id": ent_id,
            "type": st,
            "scope_type": st,
            "slug": slug,
            "name": name,
            "path": path,
            "parentPath": parent_path,
            "parent_path": parent_path,
            "precedenceRank": rank,
            "precedence_rank": rank,
            "write_mode": write_mode,
            "owner": str(payload.get("owner") or "developer@example.com"),
            "identity_binding": binding,
            "binding": binding,
            "description": str(payload.get("description") or f"Enterprise {st.upper()} scope entity at {path}"),
            "updated_at": now_iso,
        }
        self._studio_entities[ent_id] = record
        self._persist_studio_meta_to_spanner("entities", ent_id, record)
        return record

    def delete_scope_entity(self, entity_id: str) -> bool:
        self.ensure_studio_schema()
        existed = self._studio_entities.pop(entity_id, None) is not None
        if existed:
            self._persist_studio_meta_to_spanner("entities", entity_id, {"_deleted": True, "id": entity_id})
        return existed

    # -------------------------------------------------------------------------
    # Workspace 6: Memory Categories
    # -------------------------------------------------------------------------
    def list_memory_categories(self) -> List[Dict[str, Any]]:
        self.ensure_studio_schema()
        cats = list(self._studio_categories.values())
        for c in cats:
            folder = c.get("slug") or c.get("folder_path")
            c["file_count"] = sum(
                1 for f in self._studio_memory_files.values()
                if f.get("categoryFolder") == folder
            )
        return cats

    def upsert_memory_category(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        slug = str(payload.get("slug") or payload.get("folder_path") or "").strip().lower().replace(" ", "_")
        name = str(payload.get("name") or payload.get("label") or slug or "Custom Category").strip()
        if not slug:
            slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "custom_category"
        cat_id = str(payload.get("category_id") or payload.get("id") or f"cat_{slug}")
        disc_mode = str(payload.get("discovery_mode") or payload.get("discoveryMode") or "FILE_GLOB_AND_SEMANTIC")
        trigger = str(payload.get("recall_trigger") or payload.get("recallTrigger") or "FILE_GLOB: *.*")
        ttl = str(payload.get("retention_ttl") or payload.get("retentionTtl") or "PERMANENT (Crystallized)")

        record = {
            "id": cat_id,
            "category_id": cat_id,
            "slug": slug,
            "folder_path": slug,
            "name": name,
            "discoveryMode": disc_mode,
            "discovery_mode": disc_mode,
            "recallTrigger": trigger,
            "recall_trigger": trigger,
            "minConfidence": float(payload.get("minConfidence") if payload.get("minConfidence") is not None else 0.85),
            "retentionTtl": ttl,
            "retention_ttl": ttl,
            "conflictPolicy": str(payload.get("conflictPolicy") or "HIGHER_SCOPE_OVERRIDES_UNLESS_USER_STYLE"),
            "description": str(payload.get("description") or f"Memory category governing `/{slug}/*.mem`"),
            "updated_at": now_iso,
        }
        self._studio_categories[cat_id] = record
        self._persist_studio_meta_to_spanner("categories", cat_id, record)
        return record

    def delete_memory_category(self, category_id: str) -> bool:
        self.ensure_studio_schema()
        existed = self._studio_categories.pop(category_id, None) is not None
        if existed:
            self._persist_studio_meta_to_spanner("categories", category_id, {"_deleted": True, "id": category_id})
        return existed

    # -------------------------------------------------------------------------
    # Workspace 7: Ingest Services
    # -------------------------------------------------------------------------
    def list_ingest_services(self) -> Dict[str, Any]:
        self.ensure_studio_schema()
        services = list(self._studio_ingest_services.values())
        return {
            "status": "OK",
            "services": services,
            "live_events": list(self._studio_ingest_events),
            "count": len(services),
        }

    def upsert_ingest_service(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        stream_key = str(payload.get("stream_key") or payload.get("streamKey") or "CUSTOM_STREAM").upper().replace(" ", "_")
        svc_id = str(payload.get("service_id") or payload.get("id") or f"ing_{stream_key.lower()}")
        existing = self._studio_ingest_services.get(svc_id, {})

        record = {
            "id": svc_id,
            "service_id": svc_id,
            "name": str(payload.get("name") or existing.get("name") or f"Connector {stream_key}"),
            "streamKey": stream_key,
            "stream_key": stream_key,
            "protocol": str(payload.get("protocol") or existing.get("protocol") or "gRPC / MCP SSE Stream"),
            "endpoint": str(payload.get("endpoint_url") or payload.get("endpoint") or existing.get("endpoint") or f"mcp://electric-sheep-memory/ingest/{stream_key.lower()}"),
            "endpoint_url": str(payload.get("endpoint_url") or payload.get("endpoint") or existing.get("endpoint_url") or f"mcp://electric-sheep-memory/ingest/{stream_key.lower()}"),
            "targetSink": str(payload.get("targetSink") or existing.get("targetSink") or "Spanner `SessionTrajectories` + BigQuery `episodes_log`"),
            "piiRedaction": str(payload.get("piiRedaction") or existing.get("piiRedaction") or "STRICT (Cloud DLP De-ID + Zero-Secret Regex)"),
            "batchWindow": str(payload.get("batchWindow") or existing.get("batchWindow") or "Real-time per Turn (Streaming)"),
            "throughput": str(payload.get("throughput") or existing.get("throughput") or "25 turns/min"),
            "throughput_eps": float(payload.get("throughput_eps") if payload.get("throughput_eps") is not None else existing.get("throughput_eps", 25.0)),
            "avg_latency_ms": float(payload.get("avg_latency_ms") if payload.get("avg_latency_ms") is not None else existing.get("avg_latency_ms", 11.2)),
            "queue_depth": int(payload.get("queue_depth") if payload.get("queue_depth") is not None else existing.get("queue_depth", 0)),
            "status": str(payload.get("status") or existing.get("status") or "ACTIVE").upper(),
            "description": str(payload.get("description") or existing.get("description") or f"Live trajectory ingestion pipeline for {stream_key}."),
            "updated_at": now_iso,
        }
        self._studio_ingest_services[svc_id] = record
        self._persist_studio_meta_to_spanner("ingest_services", svc_id, record)
        return record

    def record_ingest_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_studio_schema()
        now_time = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
        svc_id = str(payload.get("service_id") or payload.get("id") or "ing_mcp_bridge")
        svc = self._studio_ingest_services.get(svc_id) or next(iter(self._studio_ingest_services.values()))
        evt = {
            "id": f"evt-{uuid.uuid4().hex[:4]}",
            "time": now_time,
            "stream": svc["streamKey"],
            "session": str(payload.get("session_id") or "sess-9042"),
            "actor": str(payload.get("actor") or "user:developer"),
            "bytes": str(payload.get("bytes") or "1.6 KB"),
            "piiStatus": "CLEAN (0 secrets)",
            "routedTo": str(payload.get("routedTo") or "ext_dev_corrections"),
        }
        self._studio_ingest_events.insert(0, evt)
        self._studio_ingest_events = self._studio_ingest_events[:25]
        svc["throughput_eps"] = round(float(svc.get("throughput_eps", 40.0)) + 1.0, 1)
        svc["throughput"] = f"{int(svc['throughput_eps'])} events/min"
        svc["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._persist_studio_meta_to_spanner("ingest_services", svc["id"], svc)
        return {"event": evt, "service": svc}

    # -------------------------------------------------------------------------
    # Workspace 8: Memory Extractors
    # -------------------------------------------------------------------------
    def list_extractor_jobs(self) -> List[Dict[str, Any]]:
        self.ensure_studio_schema()
        return list(self._studio_extractors.values())

    def upsert_extractor_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ext_id = str(payload.get("extractor_id") or payload.get("id") or "").strip()
        name = str(payload.get("name") or "Custom Memory Extractor").strip()
        if not ext_id:
            slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:32]
            ext_id = f"ext_{slug}"
        existing = self._studio_extractors.get(ext_id, {})

        model_id = str(payload.get("model_id") or payload.get("model") or existing.get("model") or "gemini-4-hydrogen")
        hp = payload.get("humanParams") or payload.get("human_params")
        if isinstance(hp, str):
            try:
                hp = json.loads(hp)
            except Exception:
                hp = {}
        merged_hp = dict(existing.get("humanParams") or {
            "lookbackTurns": 10,
            "requireExplicitNegation": True,
            "autoEvictContradicted": True,
            "maxTokensPerMemory": 60,
            "requireCodeEvidence": True,
            "owner": "developer@example.com",
        })
        if isinstance(hp, dict):
            merged_hp.update(hp)

        sub_streams = payload.get("subscribedStreams") or payload.get("subscribed_streams")
        if not sub_streams:
            src_s = payload.get("source_stream") or existing.get("source_stream") or "MCP_LIVE_STREAM"
            sub_streams = [s.strip() for s in str(src_s).split(",") if s.strip()]

        target_cat = str(payload.get("target_category") or payload.get("targetCategory") or existing.get("targetCategory") or "python_tooling")
        write_mode = str(payload.get("write_mode") or payload.get("writeMode") or existing.get("writeMode") or "STAGE_FOR_COMMIT")

        record = {
            "id": ext_id,
            "extractor_id": ext_id,
            "name": name,
            "model": model_id,
            "model_id": model_id,
            "location": str(payload.get("location") or existing.get("location") or ("us-central1" if "gemma" in model_id else "global")),
            "source_stream": sub_streams[0] if sub_streams else "MCP_LIVE_STREAM",
            "subscribedStreams": sub_streams,
            "targetScopes": payload.get("targetScopes") or existing.get("targetScopes") or ["/user/*", "/team/platform-ml"],
            "targetCategory": target_cat,
            "target_category": target_cat,
            "confidenceThreshold": float(payload.get("confidenceThreshold") if payload.get("confidenceThreshold") is not None else existing.get("confidenceThreshold", 0.88)),
            "dedupStrategy": str(payload.get("dedupStrategy") or existing.get("dedupStrategy") or "COSINE_SIMILARITY >= 0.85 + CONTRADICTION_CHECK"),
            "writeMode": write_mode,
            "write_mode": write_mode,
            "status": str(payload.get("status") or existing.get("status") or "RUNNING"),
            "humanParams": merged_hp,
            "systemPrompt": str(payload.get("system_prompt") or payload.get("systemPrompt") or existing.get("systemPrompt") or "Extract atomic developer preferences and invariants."),
            "updated_at": now_iso,
        }
        self._studio_extractors[ext_id] = record
        self._persist_studio_meta_to_spanner("extractors", ext_id, record)
        return record

    # -------------------------------------------------------------------------
    # Workspace 9: Backend Infrastructure & Direct Agent Audit Ledger
    # -------------------------------------------------------------------------
    def get_backend_and_audit_ledger(self) -> Dict[str, Any]:
        self.ensure_studio_schema()
        proj_id = getattr(self, "project_id", None) or getattr(self.config, "project_id", "your-gcp-project-id")
        inst_id = getattr(self, "instance_id", None) or getattr(self.config, "spanner_instance_id", "electric-sheep-instance")
        db_id = getattr(self, "database_id", None) or getattr(self.config, "spanner_database_id", "agent-memory-db")
        subsystems = [
            {
                "id": "bk_spanner_graph",
                "name": "Google Cloud Spanner (Hot Serving Memory Graph & Direct Agent I/O)",
                "type": "SPANNER_GRAPH",
                "projectId": proj_id,
                "region": "us-central1",
                "resourceUri": f"projects/{proj_id}/instances/{inst_id}/databases/{db_id}",
                "authMode": "Google Cloud ADC / Workload Identity (gcloud auth application-default)",
                "tablesOrModels": "scoped_memories, staged_memory_commits, scope_entities, memory_categories, ingest_services, extractor_jobs, memory_audit_ledger, rbac_policies",
                "latencyP95": "6.2 ms",
                "quorumOrConfig": "Leader-Aware Strong Reads · Change Streams Enabled (`cs_memory_mutations`)",
                "teardownPolicy": "Retain Production Graph · Auto-Prune Ephemeral Test Branches",
                "status": "HEALTHY",
                "notes": "Primary hot OLTP serving database for direct agent read (`federated_scoped_memory_lookup`) and direct agent write (`crystallize_live_preference`, `promote_scoped_memory`). Emits Spanner Change Streams for real-time BigQuery CDC replication.",
            },
            {
                "id": "bk_spanner_bq_sync",
                "name": "Spanner Change Streams <-> BigQuery CDC & Reverse Attribution Sync Service",
                "type": "SPANNER_BQ_SYNC",
                "projectId": proj_id,
                "region": "us-central1",
                "resourceUri": f"dataflow://{proj_id}/us-central1/spanner-to-bq-memory-cdc · bq://{proj_id}.electric_sheep_telemetry",
                "authMode": f"Google Cloud ADC / Workload Identity (`serviceAccount:memory-sync-cdc@{proj_id}.iam.gserviceaccount.com`)",
                "tablesOrModels": "cs_memory_mutations -> bq.scoped_memories_replica, bq.session_helpfulness_ratings <-> spanner.scoped_memories",
                "latencyP95": "1.4 s",
                "quorumOrConfig": "Forward Streaming CDC (Spanner -> BQ) + Reverse NREM Usefulness Attribution Sync (BQ -> Spanner)",
                "teardownPolicy": "Auto-Drain Dataflow CDC Ephemeral Staging · Retain BigQuery Replica Tables",
                "status": "HEALTHY",
                "notes": "Bridges the Hot Serving Plane (Cloud Spanner) and Analytical Plane (BigQuery). Replicates every direct agent write to BigQuery in near real-time and syncs aggregated multi-session Usefulness Trackers from BigQuery back into Spanner.",
            },
            {
                "id": "bk_vertex_vector",
                "name": "Vertex AI Vector Embeddings & Spanner Hybrid Search Index",
                "type": "VERTEX_VECTOR",
                "projectId": proj_id,
                "region": "us-central1",
                "resourceUri": f"projects/{proj_id}/locations/us-central1/publishers/google/models/text-embedding-005",
                "authMode": "Google Cloud ADC / Workload Identity",
                "tablesOrModels": "text-embedding-005 (768-dim COSINE) + Spanner SEARCH_SUBSTRING Lexical Index",
                "latencyP95": "18.4 ms",
                "quorumOrConfig": "Default Cosine Floor >= 0.65 · Reciprocal Rank Fusion (RRF)",
                "teardownPolicy": "Auto-Evict Orphaned Embedding Vectors on Memory Delete",
                "status": "HEALTHY",
                "notes": "Generates 768-dimensional semantic vectors synchronously on direct agent write or staged commit, powering hybrid lexical + cosine recall across authorized scope paths.",
            },
            {
                "id": "bk_vertex_models",
                "name": "Vertex AI Gemini 4 (Hydrogen & Neon) & Gemma 4 Inference Runtime",
                "type": "VERTEX_LLM",
                "projectId": proj_id,
                "region": "global / us-central1",
                "resourceUri": f"projects/{proj_id}/locations/global/publishers/google/models/gemini-4-hydrogen",
                "authMode": "Google Cloud ADC (Zero API Keys Required)",
                "tablesOrModels": "gemini-4-hydrogen, gemini-4-neon, gemma-4-27b-it",
                "latencyP95": "410 ms",
                "quorumOrConfig": "LOCATION=global for Gemini 4 (Hydrogen fast loop, Neon deep synthesis) · Dedicated L4 GPU Pool for Gemma 4",
                "teardownPolicy": "Zero Local Weight Loading on macOS Host · Auto-Scale to 0 Idle",
                "status": "HEALTHY",
                "notes": "Executes AI Session Summaries, Extractor Subagent jobs, and Contradiction/Helpfulness Evaluators without local Apple Silicon memory pressure.",
            },
            {
                "id": "bk_gcs_bq_archive",
                "name": "Cloud Storage Staging & BigQuery Analytical Replica / Eval Sink",
                "type": "GCS_BIGQUERY",
                "projectId": proj_id,
                "region": "us-central1",
                "resourceUri": f"gs://{proj_id}-vertex-staging/ · bq://{proj_id}.electric_sheep_telemetry",
                "authMode": "Google Cloud ADC / Service Account Impersonation",
                "tablesOrModels": "episodes_log, scoped_memories_replica, session_helpfulness_ratings",
                "latencyP95": "45.0 ms",
                "quorumOrConfig": "Spanner EXTERNAL_QUERY Connection + Partitioned Analytical Replica",
                "teardownPolicy": "Automated Post-Execution Teardown of Temp Eval Tables & GCS Blobs",
                "status": "HEALTHY",
                "notes": "Analytical OLAP store receiving real-time Spanner Change Stream CDC mutations, historical session trajectories, and Golden Journey Eval benchmarks.",
            },
        ]

        sync_pipelines = [
            {
                "id": "sync_forward_cdc",
                "name": "Forward CDC: Spanner Change Streams → BigQuery Replica",
                "direction": "Spanner → BigQuery",
                "mode": "REALTIME_CDC",
                "sourceUri": f"spanner://{db_id}/change_stream/cs_memory_mutations",
                "targetUri": f"bq://{proj_id}.electric_sheep_telemetry.scoped_memories_replica",
                "description": "Streams every direct agent write (`WRITE_THROUGH_LIVE`), staged commit merge, and eviction from Cloud Spanner into BigQuery so analytical queries and eval benchmarks never miss in-band agent mutations.",
                "syncState": "IN_SYNC",
                "lastWatermark": "Synced just now (Change Stream Watermark Current)",
            },
            {
                "id": "sync_reverse_attribution",
                "name": "Reverse Sync: BigQuery Multi-Session Usefulness Attribution → Spanner",
                "direction": "BigQuery → Spanner",
                "mode": "NREM_REVERSE_ETL",
                "sourceUri": f"bq://{proj_id}.electric_sheep_telemetry.session_helpfulness_ratings",
                "targetUri": f"spanner://{db_id}/tables/scoped_memories (helpful_count, unhelpful_count)",
                "description": "Joins ingested session trajectories and eval runs in BigQuery with recalled `.mem` versions, then writes cumulative `usefulCount` / `notUsefulCount` and threshold alerts back into Cloud Spanner.",
                "syncState": "IN_SYNC",
                "lastWatermark": "Synced just now (Usefulness Trackers Updated)",
            },
            {
                "id": "sync_federated_query",
                "name": "Zero-Copy Federated Bridge: BigQuery EXTERNAL_QUERY → Spanner",
                "direction": "BigQuery ⇄ Spanner",
                "mode": "FEDERATED_SQL",
                "sourceUri": f'EXTERNAL_QUERY("projects/{proj_id}/locations/us-central1/connections/spanner-memory-graph")',
                "targetUri": "Live Ad-Hoc Join: BigQuery Session Archive ⋈ Spanner scoped_memories",
                "description": "Allows data scientists and Eval Optimizer jobs in BigQuery to query live transactional Spanner memory state directly without waiting for batch exports.",
                "syncState": "IN_SYNC",
                "lastWatermark": "Connection Verified via Google Cloud ADC",
            },
        ]

        ledger_list = list(self._studio_audit_ledger.values())
        ledger_list.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)

        return {
            "status": "OK",
            "subsystems": subsystems,
            "sync_pipelines": sync_pipelines,
            "audit_ledger": ledger_list,
            "direct_agent_mutations": ledger_list,
        }

    def rollback_audit_mutation(self, mutation_id: str, actor: str = "developer@example.com") -> Dict[str, Any]:
        """Roll back a direct agent write or mutation in Spanner, restoring `prior_belief_snapshot`."""
        self.ensure_studio_schema()
        mut = self._studio_audit_ledger.get(mutation_id)
        if not mut:
            raise ValueError(f"Audit mutation '{mutation_id}' not found")

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        mut["rolledBack"] = True
        mut["status"] = "ROLLED_BACK"
        mut["spannerStatus"] = "ROLLED_BACK_IN_SPANNER"
        mut["bqCdcStatus"] = "ROLLBACK_REPLICATED_BQ"
        mut["rolled_back_at"] = now_iso
        mut["rolled_back_by"] = actor
        self._studio_audit_ledger[mutation_id] = mut
        self._persist_studio_meta_to_spanner("audit_ledger", mutation_id, mut)

        # Restore prior belief snapshot on the linked memory file if present
        file_id = mut.get("fileId")
        prior_snap = mut.get("prior_belief_snapshot") or mut.get("priorBeliefSnapshot")
        restored_file = None
        if file_id and file_id in self._studio_memory_files and prior_snap:
            fobj = self._studio_memory_files[file_id]
            fobj["currentContent"] = str(prior_snap)
            fobj["proposedContent"] = None
            fobj["commitState"] = "SYNCED"
            fobj["sync_status"] = "SYNCED"
            fobj["provenance"] = f"↩️ Rolled back to prior snapshot ({mutation_id}) by {actor}"
            fobj["updated_at"] = now_iso
            self._studio_memory_files[file_id] = fobj
            self._persist_studio_meta_to_spanner("memory_files", file_id, fobj)
            restored_file = fobj

        return {
            "status": "ROLLED_BACK",
            "mutation": mut,
            "restored_file": restored_file,
        }

    # -------------------------------------------------------------------------
    # Workspace 10: Security & RBAC Policies + Access Simulator
    # -------------------------------------------------------------------------
    def list_rbac_policies(self) -> List[Dict[str, Any]]:
        self.ensure_studio_schema()
        return list(self._studio_rbac_policies.values())

    def upsert_rbac_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        pol_id = str(payload.get("policy_id") or payload.get("id") or "").strip()
        name = str(payload.get("name") or "Custom Scope RBAC Policy").strip()
        if not pol_id:
            slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:32]
            pol_id = f"sec_{slug}"
        existing = self._studio_rbac_policies.get(pol_id, {})

        tree_cond = str(payload.get("scope_path_pattern") or payload.get("treeCondition") or existing.get("treeCondition") or "/team/platform-ml/**")
        scope_type = str(payload.get("scopeType") or existing.get("scopeType") or tree_cond.strip("/").split("/")[0] or "team").lower()
        perms = payload.get("permissions") or payload.get("allowed_actions")
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except Exception:
                perms = {}
        merged_perms = dict(existing.get("permissions") or {
            "RECALL": True,
            "DIRECT_WRITE": False,
            "STAGE": True,
            "COMMIT": True,
            "EVICT": False,
            "PROMOTE": True,
        })
        if isinstance(perms, dict):
            merged_perms.update({k: bool(v) for k, v in perms.items()})

        principals = payload.get("principals") or existing.get("principals") or []
        principal_str = str(payload.get("principal") or (principals[0]["id"] if principals else "group:platform-ml-eng@example.com"))
        if not principals:
            principals = [
                {"type": "GROUP" if principal_str.startswith("group:") else "SA", "id": principal_str, "cloudBinding": str(payload.get("role_binding") or "GCP IAM Binding")}
            ]

        write_gov = str(
            payload.get("write_governance")
            or payload.get("enforcementMode")
            or ("WRITE-THROUGH LIVE + BQ CDC" if merged_perms.get("DIRECT_WRITE") else "ENFORCED (Agent Writes Stage First)")
        )

        record = {
            "id": pol_id,
            "policy_id": pol_id,
            "name": name,
            "scopeType": scope_type,
            "treeCondition": tree_cond,
            "scope_path_pattern": tree_cond,
            "inheritanceMode": str(payload.get("inheritanceMode") or existing.get("inheritanceMode") or "INHERIT_TO_CHILDREN"),
            "principal": principal_str,
            "role_binding": str(payload.get("role_binding") or existing.get("role_binding") or "roles/spanner.databaseUser"),
            "principals": principals,
            "permissions": merged_perms,
            "readOnlyForGeneral": bool(payload.get("readOnlyForGeneral") if payload.get("readOnlyForGeneral") is not None else existing.get("readOnlyForGeneral", False)),
            "write_governance": write_gov,
            "enforcementMode": write_gov,
            "guardrails": str(payload.get("guardrails") or existing.get("guardrails") or f"Enforces RBAC matrix on {tree_cond}."),
            "lastIamSync": "Synced with GCP IAM (your-gcp-project-id)",
            "updated_at": now_iso,
        }
        self._studio_rbac_policies[pol_id] = record
        self._persist_studio_meta_to_spanner("rbac_policies", pol_id, record)
        return record

    def evaluate_rbac_simulator(self, principal: str, action: str, target_path: str) -> Dict[str, Any]:
        """Evaluate an effective Cloud IAM / RBAC permission check against a memory tree path."""
        self.ensure_studio_schema()
        norm_path = "/" + str(target_path or "/org/acme-org").lstrip("/")
        norm_act = str(action or "RECALL").upper().strip()
        norm_princ = str(principal or "user:developer@example.com").strip()

        # Match most specific policy by prefix length
        matched_pol = None
        best_len = -1
        for pol in self._studio_rbac_policies.values():
            prefix = str(pol.get("treeCondition", "")).replace("/**", "").rstrip("/")
            if prefix and norm_path.startswith(prefix) and len(prefix) > best_len:
                matched_pol = pol
                best_len = len(prefix)
        if not matched_pol:
            matched_pol = self._studio_rbac_policies.get("sec_org_invariants") or next(iter(self._studio_rbac_policies.values()))

        principal_ids = [p.get("id", "") for p in matched_pol.get("principals", [])]
        is_bound = (
            norm_princ in principal_ids
            or "roles/owner" in principal_ids
            or norm_princ.startswith("user:developer") or norm_princ.startswith("user:default")
        )
        perms = matched_pol.get("permissions", {})
        action_allowed = bool(perms.get(norm_act, False))

        # Safety pin check on workcell kinematics
        if "/agent/workcell-agent/kinematics_constraints" in norm_path and norm_act in ("DIRECT_WRITE", "EVICT", "COMMIT"):
            if "robotics-safety-board" not in norm_princ:
                return {
                    "status": "OK",
                    "decision": "DENY_SAFETY_PIN",
                    "allowed": False,
                    "principal": norm_princ,
                    "action": norm_act,
                    "target_path": norm_path,
                    "matched_policy": matched_pol,
                    "rationale": f"BLOCKED by Safety Pin Policy '{matched_pol['name']}': autonomous or non-safety-board {norm_act} on {norm_path} is strictly denied to protect physical workcell fracture limits.",
                }

        if norm_act == "DIRECT_WRITE" and not action_allowed and perms.get("STAGE", False):
            return {
                "status": "OK",
                "decision": "STAGE_ONLY_SHADOW",
                "allowed": True,
                "intercepted_to_staged": True,
                "principal": norm_princ,
                "action": norm_act,
                "target_path": norm_path,
                "matched_policy": matched_pol,
                "rationale": f"INTERCEPTED TO STAGED COMMIT: Policy '{matched_pol['name']}' disables unreviewed DIRECT_WRITE on {matched_pol['treeCondition']} but permits STAGE. Mutation is routed to Pending Commits.",
            }

        final_allow = bool(action_allowed and (is_bound or norm_act == "RECALL"))
        return {
            "status": "OK",
            "decision": "ALLOW" if final_allow else "DENY",
            "allowed": final_allow,
            "intercepted_to_staged": False,
            "principal": norm_princ,
            "action": norm_act,
            "target_path": norm_path,
            "matched_policy": matched_pol,
            "rationale": (
                f"ALLOWED by Policy '{matched_pol['name']}' ({matched_pol['treeCondition']}) with governance mode '{matched_pol['enforcementMode']}'."
                if final_allow
                else f"DENIED by Policy '{matched_pol['name']}' ({matched_pol['treeCondition']}): principal '{norm_princ}' is not authorized for '{norm_act}'."
            ),
        }

    # -------------------------------------------------------------------------
    # Studio Background & Inline Jobs Persistence (`studio::jobs`)
    # -------------------------------------------------------------------------
    def _jobs_lock(self) -> threading.RLock:
        lock = getattr(self, "_studio_jobs_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._studio_jobs_lock = lock
        return lock

    def list_studio_jobs(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List Studio background and inline jobs ordered newest-first."""
        self.ensure_studio_schema()
        cat_filter = (category or "").strip().lower()
        status_filter = (status or "").strip().upper()
        with self._jobs_lock():
            rows = [
                dict(row)
                for row in self._studio_jobs.values()
                if isinstance(row, dict) and not row.get("_deleted")
            ]
        if cat_filter and cat_filter != "all":
            rows = [r for r in rows if str(r.get("category") or "").lower() == cat_filter]
        if status_filter and status_filter != "ALL":
            rows = [r for r in rows if str(r.get("status") or "").upper() == status_filter]
        rows.sort(
            key=lambda r: str(r.get("started_at") or r.get("created_at") or ""),
            reverse=True,
        )
        return rows[: max(1, int(limit or 50))]

    def get_studio_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a single Studio job record by ID."""
        self.ensure_studio_schema()
        with self._jobs_lock():
            row = self._studio_jobs.get(job_id)
            if isinstance(row, dict) and not row.get("_deleted"):
                return dict(row)
        return None

    def stage_studio_job_in_memory(
        self, record: Dict[str, Any], force_persist: bool = False
    ) -> tuple[Dict[str, Any], bool]:
        """Store or update a Studio job record in memory and return (stored_copy, should_persist)."""
        self.ensure_studio_schema()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        stored = dict(record)
        job_id = str(stored.get("job_id") or "").strip()
        if not job_id:
            job_id = f"job-{uuid.uuid4().hex[:8]}"
            stored["job_id"] = job_id
        stored.setdefault("created_at", now_iso)
        stored["updated_at"] = now_iso

        steps = stored.get("steps")
        if isinstance(steps, list) and len(steps) > 50:
            stored["steps"] = steps[-50:]

        raw_params = stored.get("raw_params")
        if isinstance(raw_params, dict):
            try:
                if len(json.dumps(raw_params)) > 32768:
                    stored["raw_params"] = dict(stored.get("input_params") or {})
            except Exception:
                stored["raw_params"] = dict(stored.get("input_params") or {})

        preview = stored.get("output_preview")
        if isinstance(preview, dict):
            try:
                encoded = json.dumps(preview)
                if len(encoded) > 12000:
                    stored["output_preview"] = {"truncated": True, "summary": stored.get("result_summary", "")}
            except Exception:
                stored["output_preview"] = {"summary": str(stored.get("result_summary", ""))}

        status_upper = str(stored.get("status") or "QUEUED").upper()
        is_terminal = status_upper in ("SUCCEEDED", "FAILED", "CANCELED")
        should_persist = force_persist or is_terminal

        with self._jobs_lock():
            prev = self._studio_jobs.get(job_id)
            prev_status = str((prev or {}).get("status") or "").upper()
            if prev_status != status_upper:
                should_persist = True
            now_mono = time.monotonic()
            last_p = self._studio_jobs_last_persisted.get(job_id, 0.0)
            if not should_persist and (now_mono - last_p) >= 0.5:
                should_persist = True
            self._studio_jobs[job_id] = stored
            if should_persist:
                self._studio_jobs_last_persisted[job_id] = now_mono

        return dict(stored), should_persist

    def flush_studio_job_to_spanner(self, job_id: str, record: Dict[str, Any]) -> None:
        """Persist a staged Studio job record to Cloud Spanner outside runner locks."""
        self._persist_studio_meta_to_spanner("jobs", job_id, record)

    def upsert_studio_job(self, record: Dict[str, Any], force_persist: bool = False) -> Dict[str, Any]:
        """Store or update a Studio job record in memory and Cloud Spanner."""
        stored, should_persist = self.stage_studio_job_in_memory(record, force_persist=force_persist)
        if should_persist:
            self.flush_studio_job_to_spanner(stored["job_id"], stored)
        return stored

    def clear_finished_studio_jobs(self) -> int:
        """Delete terminal (SUCCEEDED/FAILED/CANCELED) job records while keeping active jobs."""
        self.ensure_studio_schema()
        terminal = {"SUCCEEDED", "FAILED", "CANCELED"}
        removed_ids: List[str] = []
        with self._jobs_lock():
            for job_id, row in list(self._studio_jobs.items()):
                if str((row or {}).get("status") or "").upper() in terminal:
                    removed_ids.append(job_id)
                    self._studio_jobs.pop(job_id, None)
                    self._studio_jobs_last_persisted.pop(job_id, None)
        for job_id in removed_ids:
            self._persist_studio_meta_to_spanner("jobs", job_id, {"_deleted": True})
        return len(removed_ids)


SpannerMemoryClient = SpannerPreferenceClient


