"""Built-In AlphaEvolve Auto-Tuner for Enterprise Memory Studio.

Implements Google DeepMind's AlphaEvolve architecture (MAP-Elites Quality-Diversity Archive +
Parent/Inspiration Sampling + Diagnostic-Guided LLM Genome Mutation + Multi-Objective Pareto Selection)
to automatically optimize Memory & Vector configurations for maximum Quality and minimum Token usage.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from google import genai
from google.genai import types

from src.common.config import get_config
from src.common.spanner_client import MATRYOSHKA_DIMS, SpannerPreferenceClient
from src.studio.eval_engine import EvalCase, EvalEngine

logger = logging.getLogger("adk.studio.alpha_evolve")

TOKEN_NICHES = ("ULTRA_COMPACT", "BALANCED", "DEEP_CONTEXT")
VECTOR_NICHES = ("FAST_VEC", "FULL_VEC")
INJECTION_FORMATS = ("ATOMIC_KV", "COMPACT_BULLET", "FULL_BELIEF")


@dataclass
class MemoryConfig:
    """Evolutionary Genome representing a complete Enterprise Memory & Vector Configuration."""

    config_id: str = field(default_factory=lambda: f"cfg-{uuid.uuid4().hex[:8]}")
    parent_id: Optional[str] = None
    inspiration_id: Optional[str] = None
    generation: int = 0
    embedding_dimensions: int = 768  # 128 | 256 | 512 | 768
    retrieval_cosine_threshold: float = 0.60  # 0.25 to 0.70
    certification_min_confidence: float = 0.85  # 0.70 to 0.95
    semantic_dedup_threshold: float = 0.20  # 0.10 to 0.30
    top_k_per_scope: int = 4  # 1 to 6
    injection_format: str = "FULL_BELIEF"  # ATOMIC_KV | COMPACT_BULLET | FULL_BELIEF
    scope_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "ORG": 1.0,
            "TEAM": 0.85,
            "AGENT": 0.80,
            "USER": 0.95,
        }
    )
    rem_extraction_prompt_addon: str = (
        "Extract grounded high-confidence beliefs and preserve explicit user overrides."
    )
    mutation_rationale: str = "Baseline initial configuration"
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if self.embedding_dimensions not in MATRYOSHKA_DIMS:
            # Snap to nearest valid Matryoshka dimension
            self.embedding_dimensions = min(
                MATRYOSHKA_DIMS, key=lambda d: abs(d - int(self.embedding_dimensions))
            )
        self.retrieval_cosine_threshold = round(
            max(0.25, min(0.75, float(self.retrieval_cosine_threshold))), 3
        )
        self.certification_min_confidence = round(
            max(0.70, min(0.95, float(self.certification_min_confidence))), 3
        )
        self.semantic_dedup_threshold = round(
            max(0.10, min(0.30, float(self.semantic_dedup_threshold))), 3
        )
        self.top_k_per_scope = max(1, min(6, int(self.top_k_per_scope)))
        fmt = str(self.injection_format).upper().strip()
        self.injection_format = fmt if fmt in INJECTION_FORMATS else "COMPACT_BULLET"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryConfig":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class MAPElitesArchive:
    """2D Quality-Diversity MAP-Elites Grid indexed by (Token Budget Niche, Vector Dimensionality Niche).

    - Axis 1 (Token Budget Niche):
      - "ULTRA_COMPACT": avg_tokens_used < 45
      - "BALANCED": 45 <= avg_tokens_used <= 110
      - "DEEP_CONTEXT": avg_tokens_used > 110
    - Axis 2 (Vector Dimensionality Niche):
      - "FAST_VEC": 128d or 256d
      - "FULL_VEC": 512d or 768d
    """

    def __init__(self) -> None:
        self.grid: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.history: List[Dict[str, Any]] = []
        self._sample_cursor: int = 0

    @staticmethod
    def classify_niche(config: MemoryConfig, avg_tokens_used: float) -> Tuple[str, str]:
        """Map a MemoryConfig and its empirical token footprint to its (token_niche, vector_niche) cell."""
        if avg_tokens_used < 45.0:
            token_niche = "ULTRA_COMPACT"
        elif avg_tokens_used <= 110.0:
            token_niche = "BALANCED"
        else:
            token_niche = "DEEP_CONTEXT"

        vector_niche = "FAST_VEC" if config.embedding_dimensions in (128, 256) else "FULL_VEC"
        return (token_niche, vector_niche)

    def insert(self, config: MemoryConfig, eval_result: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a candidate MemoryConfig and its evaluation metrics into the MAP-Elites grid."""
        avg_tokens = float(eval_result.get("avg_tokens_used", 60.0))
        niche = self.classify_niche(config, avg_tokens)
        fitness = float(eval_result.get("pareto_fitness", 0.0))
        quality = float(eval_result.get("quality_score", 0.0))
        efficiency = float(eval_result.get("token_efficiency_score", 0.0))

        entry = {
            "config": config.to_dict(),
            "config_obj": config,
            "niche": {"token_niche": niche[0], "vector_niche": niche[1]},
            "niche_key": f"{niche[0]}::{niche[1]}",
            "quality_score": quality,
            "token_efficiency_score": efficiency,
            "pareto_fitness": fitness,
            "avg_tokens_used": avg_tokens,
            "passed_cases": eval_result.get("passed_cases", 0),
            "total_cases": eval_result.get("total_cases", 0),
            "metrics_breakdown": eval_result.get("metrics_breakdown", {}),
            "diagnostics": eval_result.get("diagnostics", []),
            "is_new_elite": False,
        }

        existing = self.grid.get(niche)
        if existing is None or fitness >= float(existing.get("pareto_fitness", -1.0)):
            entry["is_new_elite"] = True
            self.grid[niche] = entry

        self.history.append(entry)
        return entry

    def sample_parent_and_inspiration(self) -> Tuple[MemoryConfig, Optional[MemoryConfig], List[Dict[str, Any]]]:
        """Sample a high-performing Parent config, a complementary Inspiration config, and failed diagnostics."""
        if not self.grid:
            default_cfg = MemoryConfig()
            return default_cfg, None, []

        elites = sorted(
            self.grid.values(),
            key=lambda e: float(e.get("pareto_fitness", 0.0)),
            reverse=True,
        )
        parent_entry = elites[self._sample_cursor % len(elites)]
        self._sample_cursor += 1
        parent_cfg: MemoryConfig = parent_entry["config_obj"]

        inspiration_cfg: Optional[MemoryConfig] = None
        if len(elites) > 1:
            # Pick an elite from a different niche (e.g. highest token efficiency or complementary vector dim)
            by_eff = sorted(
                [e for e in elites if e["config"]["config_id"] != parent_cfg.config_id],
                key=lambda e: float(e.get("token_efficiency_score", 0.0)),
                reverse=True,
            )
            if by_eff:
                inspiration_cfg = by_eff[0]["config_obj"]

        failed_diags = [
            d for d in parent_entry.get("diagnostics", []) if not d.get("passed", True)
        ]
        return parent_cfg, inspiration_cfg, failed_diags

    def get_champion(self) -> Optional[Dict[str, Any]]:
        """Return the highest Pareto-fitness elite across the entire archive."""
        if not self.grid:
            return None
        best = max(self.grid.values(), key=lambda e: (float(e.get("pareto_fitness", 0.0)), float(e.get("quality_score", 0.0))))
        return self._serialize_entry(best)

    def get_pareto_frontier(self) -> List[Dict[str, Any]]:
        """Compute the non-dominated Pareto frontier across (quality_score, token_efficiency_score)."""
        candidates = list(self.history)
        frontier: List[Dict[str, Any]] = []
        for cand in candidates:
            q_c = float(cand.get("quality_score", 0.0))
            t_c = float(cand.get("token_efficiency_score", 0.0))
            dominated = False
            for other in candidates:
                if other["config"]["config_id"] == cand["config"]["config_id"]:
                    continue
                q_o = float(other.get("quality_score", 0.0))
                t_o = float(other.get("token_efficiency_score", 0.0))
                if (q_o >= q_c and t_o >= t_c) and (q_o > q_c or t_o > t_c):
                    dominated = True
                    break
            if not dominated:
                frontier.append(self._serialize_entry(cand))

        frontier.sort(key=lambda x: (x["quality_score"], x["token_efficiency_score"]), reverse=True)
        return frontier

    @staticmethod
    def _serialize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            k: v for k, v in entry.items() if k != "config_obj"
        }

    def to_dict(self) -> Dict[str, Any]:
        cells: Dict[str, Any] = {}
        for t_niche in TOKEN_NICHES:
            for v_niche in VECTOR_NICHES:
                key = f"{t_niche}::{v_niche}"
                cell = self.grid.get((t_niche, v_niche))
                cells[key] = self._serialize_entry(cell) if cell else None
        return {
            "token_niches": list(TOKEN_NICHES),
            "vector_niches": list(VECTOR_NICHES),
            "cells": cells,
            "populated_cells": len(self.grid),
            "total_cells": len(TOKEN_NICHES) * len(VECTOR_NICHES),
            "total_evaluations": len(self.history),
            "champion": self.get_champion(),
            "pareto_frontier": self.get_pareto_frontier(),
            "history": [self._serialize_entry(h) for h in self.history],
        }


# Global studio state for active config & latest archive
_GLOBAL_ACTIVE_CONFIG: Optional[MemoryConfig] = None
_GLOBAL_ARCHIVE: Optional[MAPElitesArchive] = None


class AlphaEvolveEngine:
    """AlphaEvolve Evolutionary Optimizer for Enterprise Memory & Vector Configurations."""

    ACTIVE_CONFIG_SCOPE_TYPE = "ORG"
    ACTIVE_CONFIG_SCOPE_ID = "studio:active_config"
    ACTIVE_CONFIG_KEY = "active_memory_genome"

    def __init__(
        self,
        spanner_client: Optional[SpannerPreferenceClient] = None,
        eval_engine: Optional[EvalEngine] = None,
        use_mock: Optional[bool] = None,
    ):
        global _GLOBAL_ARCHIVE, _GLOBAL_ACTIVE_CONFIG
        self.config = get_config()
        self.use_mock = False if use_mock is None else use_mock
        self.spanner_client = spanner_client or SpannerPreferenceClient(use_mock=self.use_mock)
        self.eval_engine = eval_engine or EvalEngine(
            spanner_client=self.spanner_client,
            use_mock=self.use_mock,
        )
        if _GLOBAL_ARCHIVE is None:
            _GLOBAL_ARCHIVE = MAPElitesArchive()
        self.archive: MAPElitesArchive = _GLOBAL_ARCHIVE
        self._evolution_lock = threading.Lock()
        if _GLOBAL_ACTIVE_CONFIG is None:
            _GLOBAL_ACTIVE_CONFIG = self._load_active_config_from_spanner() or MemoryConfig(
                config_id="cfg-baseline-0",
                generation=0,
                embedding_dimensions=768,
                retrieval_cosine_threshold=0.60,
                certification_min_confidence=0.85,
                semantic_dedup_threshold=0.20,
                top_k_per_scope=4,
                injection_format="FULL_BELIEF",
            )

    def _load_active_config_from_spanner(self) -> Optional[MemoryConfig]:
        rec = self.spanner_client.get_scoped_memory(
            scope_type=self.ACTIVE_CONFIG_SCOPE_TYPE,
            scope_id=self.ACTIVE_CONFIG_SCOPE_ID,
            memory_key=self.ACTIVE_CONFIG_KEY,
        )
        if rec and rec.get("memory_value"):
            try:
                data = json.loads(rec["memory_value"])
                if isinstance(data, dict):
                    return MemoryConfig.from_dict(data)
            except Exception:
                pass
        return None

    def get_active_config(self) -> MemoryConfig:
        global _GLOBAL_ACTIVE_CONFIG
        if _GLOBAL_ACTIVE_CONFIG is None:
            _GLOBAL_ACTIVE_CONFIG = MemoryConfig()
        return _GLOBAL_ACTIVE_CONFIG

    def promote_to_active_config(self, config: Optional[MemoryConfig | Dict[str, Any]] = None) -> Dict[str, Any]:
        """Promote a Champion or selected MemoryConfig to the active Studio production genome in Spanner."""
        global _GLOBAL_ACTIVE_CONFIG
        if config is None:
            champ = self.archive.get_champion()
            if champ and champ.get("config"):
                target_cfg = MemoryConfig.from_dict(champ["config"])
            else:
                target_cfg = self.get_active_config()
        elif isinstance(config, dict):
            target_cfg = MemoryConfig.from_dict(config)
        else:
            target_cfg = config

        _GLOBAL_ACTIVE_CONFIG = target_cfg
        self.spanner_client.upsert_scoped_memory(
            scope_type=self.ACTIVE_CONFIG_SCOPE_TYPE,
            scope_id=self.ACTIVE_CONFIG_SCOPE_ID,
            memory_key=self.ACTIVE_CONFIG_KEY,
            memory_value=json.dumps(target_cfg.to_dict()),
            memory_type="PROCEDURAL_SKILL",
            category="studio_config",
            confidence=1.0,
            crystallized_belief=(
                f"Active Enterprise Memory Studio genome {target_cfg.config_id}: "
                f"{target_cfg.embedding_dimensions}d vectors, {target_cfg.injection_format} format."
            ),
            keywords="alpha_evolve, memory_config, matryoshka, studio",
            source_session_id="ALPHAEVOLVE_CHAMPION_PROMOTION",
        )
        return {
            "status": "PROMOTED_TO_ACTIVE",
            "active_config": target_cfg.to_dict(),
        }

    def propose_mutation(
        self,
        parent: MemoryConfig,
        inspiration: Optional[MemoryConfig],
        failed_diagnostics: List[Dict[str, Any]],
        generation: int,
        variant_idx: int = 0,
    ) -> MemoryConfig:
        """Propose an evolutionary child genome using Gemini 4 Neon (Vertex AI ADC) + diagnostic guidance."""
        mutation_prompt = f"""
You are AlphaEvolve, an evolutionary meta-optimizer for an Enterprise Agent Memory System backed by Google Cloud Spanner.
Your objective is to mutate the Parent MemoryConfig genome (drawing crossover traits from the Inspiration genome if present)
to simultaneously maximize Recall/Guardrail Quality Score AND maximize Token/Vector Efficiency.

Parent Genome (Generation {parent.generation}):
{json.dumps(parent.to_dict(), indent=2)}

Inspiration Genome:
{json.dumps(inspiration.to_dict(), indent=2) if inspiration else "None"}

Failed Evaluation Diagnostics to Fix:
{json.dumps(failed_diagnostics, indent=2)}

Target Generation: {generation} (Variant #{variant_idx + 1})

Available Mutation Space:
- "embedding_dimensions": choose from [128, 256, 512, 768] (128 or 256 gives higher Matryoshka compression; 512 or 768 gives fuller semantic resolution)
- "retrieval_cosine_threshold": float between 0.35 and 0.68
- "certification_min_confidence": float between 0.75 and 0.92 (must reject low-confidence hallucinations ~0.55 while certifying valid ~0.95 facts)
- "semantic_dedup_threshold": float between 0.12 and 0.28
- "top_k_per_scope": int between 2 and 5
- "injection_format": choose from ["ATOMIC_KV", "COMPACT_BULLET", "FULL_BELIEF"] ("ATOMIC_KV" reduces tokens by ~70%; "COMPACT_BULLET" balances readability and tokens)
- "rem_extraction_prompt_addon": concise 1-sentence instruction for REM belief crystallization
- "mutation_rationale": 1-sentence explanation of why this mutation improves Pareto fitness

Return a JSON object with the mutated genome parameters.
"""
        # Deterministic evolutionary progressionschedule if running in unit test mock mode or as structured priors
        schedule_presets = [
            {
                "embedding_dimensions": 256,
                "retrieval_cosine_threshold": 0.58,
                "certification_min_confidence": 0.85,
                "semantic_dedup_threshold": 0.18,
                "top_k_per_scope": 3,
                "injection_format": "COMPACT_BULLET",
                "rem_extraction_prompt_addon": (
                    "Distill atomic key-value beliefs with explicit scope precedence and reject speculative claims <0.85."
                ),
                "mutation_rationale": (
                    "Compressed vectors to 256d (FAST_VEC) and switched from FULL_BELIEF to COMPACT_BULLET to cut context tokens by ~55%."
                ),
            },
            {
                "embedding_dimensions": 128,
                "retrieval_cosine_threshold": 0.62,
                "certification_min_confidence": 0.86,
                "semantic_dedup_threshold": 0.16,
                "top_k_per_scope": 3,
                "injection_format": "ATOMIC_KV",
                "rem_extraction_prompt_addon": (
                    "Synthesize ultra-compact key=value beliefs, enforce ORG invariant guardrails, and discard hypotheticals."
                ),
                "mutation_rationale": (
                    "Evolved to 128d Matryoshka vectors + ATOMIC_KV format (~70% token reduction) in ULTRA_COMPACT::FAST_VEC niche while preserving 100% recall."
                ),
            },
            {
                "embedding_dimensions": 512,
                "retrieval_cosine_threshold": 0.60,
                "certification_min_confidence": 0.88,
                "semantic_dedup_threshold": 0.20,
                "top_k_per_scope": 4,
                "injection_format": "ATOMIC_KV",
                "rem_extraction_prompt_addon": (
                    "Preserve 512d semantic precision while formatting context as ultra-dense atomic key=value tokens."
                ),
                "mutation_rationale": (
                    "Crossover between FULL_VEC (512d) and ULTRA_COMPACT (ATOMIC_KV) to populate the ULTRA_COMPACT::FULL_VEC MAP-Elites cell."
                ),
            },
            {
                "embedding_dimensions": 256,
                "retrieval_cosine_threshold": 0.60,
                "certification_min_confidence": 0.87,
                "semantic_dedup_threshold": 0.19,
                "top_k_per_scope": 4,
                "injection_format": "ATOMIC_KV",
                "rem_extraction_prompt_addon": (
                    "Prioritize latest user overrides for preferences while locking ORG invariant guardrails."
                ),
                "mutation_rationale": (
                    "Fine-tuned 256d cosine threshold (0.60) and certification gate (0.87) for maximum Pareto fitness."
                ),
            },
        ]
        preset = dict(schedule_presets[(generation + variant_idx - 1) % len(schedule_presets)])

        if not self.use_mock:
            try:
                client = genai.Client(vertexai=True, project=self.config.project_id, location="global")
                response = client.models.generate_content(
                    model=self.config.reasoning_model_name,
                    contents=mutation_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.35,
                    ),
                )
                llm_data = json.loads(response.text or "{}")
                if isinstance(llm_data, dict):
                    for k in (
                        "embedding_dimensions",
                        "retrieval_cosine_threshold",
                        "certification_min_confidence",
                        "semantic_dedup_threshold",
                        "top_k_per_scope",
                        "injection_format",
                        "rem_extraction_prompt_addon",
                        "mutation_rationale",
                    ):
                        if k in llm_data and llm_data[k] is not None:
                            preset[k] = llm_data[k]
            except Exception as e:
                logger.warning(f"Gemini AlphaEvolve mutation call fell back to evolutionary schedule ({e})")
                preset["mutation_rationale"] = f"[Schedule prior fallback: {e}] {preset['mutation_rationale']}"

        child = MemoryConfig(
            config_id=f"cfg-gen{generation}-{uuid.uuid4().hex[:6]}",
            parent_id=parent.config_id,
            inspiration_id=inspiration.config_id if inspiration else None,
            generation=generation,
            embedding_dimensions=int(preset["embedding_dimensions"]),
            retrieval_cosine_threshold=float(preset["retrieval_cosine_threshold"]),
            certification_min_confidence=float(preset["certification_min_confidence"]),
            semantic_dedup_threshold=float(preset["semantic_dedup_threshold"]),
            top_k_per_scope=int(preset["top_k_per_scope"]),
            injection_format=str(preset["injection_format"]),
            scope_weights=dict(parent.scope_weights),
            rem_extraction_prompt_addon=str(preset["rem_extraction_prompt_addon"]),
            mutation_rationale=str(preset["mutation_rationale"]),
        )
        return child

    def iter_evolution(
        self,
        generations: int = 3,
        alpha_quality_weight: float = 0.65,
        eval_cases: Optional[List[EvalCase]] = None,
        candidates_per_generation: int = 2,
        auto_apply_min_gain: Optional[float] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Stream real-time AlphaEvolve optimization events across `generations` generations.

        Yields structured progress dictionaries (`start`, `phase`, `baseline`, `candidate`, `complete`)
        at every real mutation and evaluation step. If `auto_apply_min_gain` is set and the final
        champion beats baseline fitness by at least that threshold, the champion is automatically
        promoted to the active runtime configuration.
        """
        if eval_cases is None:
            all_cases = self.eval_engine.list_eval_cases()
            if not all_cases:
                eval_cases = self.eval_engine.generate_adversarial_eval_cases(count=4)
            elif len(all_cases) > 8:
                eval_cases = None  # Let run_evaluation_suite use its bounded 8-case representative subset
            else:
                eval_cases = all_cases

        total_gens = max(1, int(generations))
        cands_per_gen = max(1, int(candidates_per_generation))
        total_steps = 1 + (total_gens * cands_per_gen)
        completed_steps = 0

        yield {
            "type": "start",
            "step": 0,
            "total_steps": total_steps,
            "generations": total_gens,
            "candidates_per_generation": cands_per_gen,
            "alpha_quality_weight": float(alpha_quality_weight),
            "auto_apply_min_gain": float(auto_apply_min_gain) if auto_apply_min_gain is not None else None,
            "message": f"Starting evolution ({total_gens} gens · {cands_per_gen} candidates/gen)…",
        }

        # Generation 0: Baseline uncompressed configuration (FULL_BELIEF, 768d)
        baseline_cfg = MemoryConfig(
            config_id=f"cfg-gen0-base-{uuid.uuid4().hex[:4]}",
            parent_id=None,
            inspiration_id=None,
            generation=0,
            embedding_dimensions=768,
            retrieval_cosine_threshold=0.58,
            certification_min_confidence=0.85,
            semantic_dedup_threshold=0.20,
            top_k_per_scope=5,
            injection_format="FULL_BELIEF",
            mutation_rationale="Generation 0 uncompressed baseline (768d FULL_BELIEF)",
        )
        yield {
            "type": "phase",
            "step": completed_steps,
            "total_steps": total_steps,
            "generation": 0,
            "candidate": 0,
            "phase": "baseline",
            "config": baseline_cfg.to_dict(),
            "message": "Evaluating baseline (768d · Full Belief)…",
        }
        base_eval = self.eval_engine.run_evaluation_suite(
            config=baseline_cfg,
            eval_cases=eval_cases,
            alpha_quality_weight=alpha_quality_weight,
        )
        gen0_entry = self.archive.insert(baseline_cfg, base_eval)
        run_entries: List[Dict[str, Any]] = [gen0_entry]
        completed_steps += 1
        baseline_serialized = MAPElitesArchive._serialize_entry(gen0_entry)
        baseline_fitness = float(gen0_entry["pareto_fitness"])
        baseline_tokens = float(gen0_entry["avg_tokens_used"])

        yield {
            "type": "baseline",
            "step": completed_steps,
            "total_steps": total_steps,
            "generation": 0,
            "entry": baseline_serialized,
            "message": f"Baseline scored {baseline_fitness:.3f} ({baseline_tokens:.0f} avg tokens)",
        }

        generation_summaries: List[Dict[str, Any]] = [
            {
                "generation": 0,
                "best_config_id": baseline_cfg.config_id,
                "quality_score": gen0_entry["quality_score"],
                "token_efficiency_score": gen0_entry["token_efficiency_score"],
                "pareto_fitness": gen0_entry["pareto_fitness"],
                "avg_tokens_used": gen0_entry["avg_tokens_used"],
                "embedding_dimensions": baseline_cfg.embedding_dimensions,
                "injection_format": baseline_cfg.injection_format,
                "mutation_rationale": baseline_cfg.mutation_rationale,
            }
        ]

        for gen in range(1, total_gens + 1):
            gen_entries: List[Dict[str, Any]] = []
            for v_idx in range(cands_per_gen):
                cand_num = v_idx + 1
                with self._evolution_lock:
                    parent_cfg, inspiration_cfg, failed_diags = self.archive.sample_parent_and_inspiration()
                yield {
                    "type": "phase",
                    "step": completed_steps,
                    "total_steps": total_steps,
                    "generation": gen,
                    "candidate": cand_num,
                    "candidates_in_gen": cands_per_gen,
                    "phase": "mutating",
                    "parent_id": parent_cfg.config_id,
                    "inspiration_id": inspiration_cfg.config_id if inspiration_cfg else None,
                    "message": f"Gen {gen}/{total_gens} · Candidate {cand_num}/{cands_per_gen} · Mutating settings…",
                }
                mutant_cfg = self.propose_mutation(
                    parent=parent_cfg,
                    inspiration=inspiration_cfg,
                    failed_diagnostics=failed_diags,
                    generation=gen,
                    variant_idx=v_idx,
                )
                yield {
                    "type": "phase",
                    "step": completed_steps,
                    "total_steps": total_steps,
                    "generation": gen,
                    "candidate": cand_num,
                    "candidates_in_gen": cands_per_gen,
                    "phase": "evaluating",
                    "config": mutant_cfg.to_dict(),
                    "message": (
                        f"Gen {gen}/{total_gens} · Candidate {cand_num}/{cands_per_gen} · "
                        f"Scoring {mutant_cfg.embedding_dimensions}d {mutant_cfg.injection_format}…"
                    ),
                }
                mutant_eval = self.eval_engine.run_evaluation_suite(
                    config=mutant_cfg,
                    eval_cases=eval_cases,
                    alpha_quality_weight=alpha_quality_weight,
                )
                with self._evolution_lock:
                    inserted = self.archive.insert(mutant_cfg, mutant_eval)
                gen_entries.append(inserted)
                run_entries.append(inserted)
                completed_steps += 1

                best_run_entry = max(
                    enumerate(run_entries),
                    key=lambda pair: (float(pair[1]["pareto_fitness"]), pair[0]),
                )[1]
                current_champion = MAPElitesArchive._serialize_entry(best_run_entry)
                champ_fit = float(current_champion["pareto_fitness"])
                champ_toks = float(current_champion["avg_tokens_used"])
                cur_gain = round(champ_fit - baseline_fitness, 4)
                cur_tok_pct = (
                    round((1.0 - (champ_toks / max(1.0, baseline_tokens))) * 100.0, 1)
                    if baseline_tokens > 0
                    else 0.0
                )
                inserted_serialized = MAPElitesArchive._serialize_entry(inserted)
                yield {
                    "type": "candidate",
                    "step": completed_steps,
                    "total_steps": total_steps,
                    "generation": gen,
                    "candidate": cand_num,
                    "candidates_in_gen": cands_per_gen,
                    "entry": inserted_serialized,
                    "is_new_elite": bool(inserted.get("is_new_elite", False)),
                    "champion": current_champion,
                    "fitness_improvement": cur_gain,
                    "token_reduction_pct": cur_tok_pct,
                    "message": (
                        f"Gen {gen}/{total_gens} · Candidate {cand_num}/{cands_per_gen} · "
                        f"{'New elite' if inserted.get('is_new_elite') else 'Evaluated'} "
                        f"({inserted_serialized['pareto_fitness']:.3f})"
                    ),
                }

            best_in_gen = max(gen_entries, key=lambda e: float(e["pareto_fitness"]))
            generation_summaries.append({
                "generation": gen,
                "best_config_id": best_in_gen["config"]["config_id"],
                "parent_id": best_in_gen["config"]["parent_id"],
                "inspiration_id": best_in_gen["config"]["inspiration_id"],
                "quality_score": best_in_gen["quality_score"],
                "token_efficiency_score": best_in_gen["token_efficiency_score"],
                "pareto_fitness": best_in_gen["pareto_fitness"],
                "avg_tokens_used": best_in_gen["avg_tokens_used"],
                "embedding_dimensions": best_in_gen["config"]["embedding_dimensions"],
                "injection_format": best_in_gen["config"]["injection_format"],
                "mutation_rationale": best_in_gen["config"]["mutation_rationale"],
            })

        best_run_entry = max(
            enumerate(run_entries),
            key=lambda pair: (float(pair[1]["pareto_fitness"]), pair[0]),
        )[1]
        champion = MAPElitesArchive._serialize_entry(best_run_entry)
        champion_fitness = float(champion["pareto_fitness"])
        fitness_gain = round(champion_fitness - baseline_fitness, 4)

        champion_tokens = float(champion["avg_tokens_used"])
        token_reduction_pct = (
            round((1.0 - (champion_tokens / max(1.0, baseline_tokens))) * 100.0, 1)
            if baseline_tokens > 0
            else 0.0
        )

        auto_applied = False
        auto_apply_reason: Optional[str] = None
        if auto_apply_min_gain is not None and champion and champion.get("config"):
            threshold = float(auto_apply_min_gain)
            champ_id = champion["config"].get("config_id")
            beat_baseline = champ_id != baseline_cfg.config_id and fitness_gain > 0.0
            if beat_baseline and fitness_gain >= threshold:
                with self._evolution_lock:
                    self.promote_to_active_config(MemoryConfig.from_dict(champion["config"]))
                auto_applied = True
                auto_apply_reason = (
                    f"Auto-applied winner ({fitness_gain:+.4f} >= {threshold:+.4f} threshold)"
                )
            elif not beat_baseline:
                auto_apply_reason = (
                    f"Held for review (no candidate beat baseline; {fitness_gain:+.4f})"
                )
            else:
                auto_apply_reason = (
                    f"Held for review ({fitness_gain:+.4f} < {threshold:+.4f} threshold)"
                )

        final_result = {
            "status": "EVOLUTION_COMPLETE",
            "generations_run": total_gens,
            "alpha_quality_weight": alpha_quality_weight,
            "baseline": baseline_serialized,
            "champion": champion,
            "fitness_improvement": fitness_gain,
            "token_reduction_pct": token_reduction_pct,
            "generation_trajectory": generation_summaries,
            "archive": self.archive.to_dict(),
            "auto_applied": auto_applied,
            "auto_apply_min_gain": float(auto_apply_min_gain) if auto_apply_min_gain is not None else None,
            "auto_apply_reason": auto_apply_reason,
            "active_config": self.get_active_config().to_dict(),
        }

        yield {
            "type": "complete",
            "step": total_steps,
            "total_steps": total_steps,
            "result": final_result,
            "message": auto_apply_reason or f"Evolution complete ({fitness_gain:+.4f} score gain)",
        }

    def run_evolution(
        self,
        generations: int = 3,
        alpha_quality_weight: float = 0.65,
        eval_cases: Optional[List[EvalCase]] = None,
        candidates_per_generation: int = 2,
        auto_apply_min_gain: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute an AlphaEvolve optimization run across `generations` generations.

        1. Evaluates the Generation 0 baseline configuration (`FULL_BELIEF`, `768d`).
        2. Iteratively samples Parent + Inspiration from the MAP-Elites archive, mutates via Gemini 4 Neon,
           evaluates on the Multi-Objective EvalEngine, and updates the MAP-Elites grid.
        3. Identifies the overall Pareto Champion configuration (and optionally auto-promotes it
           if `auto_apply_min_gain` is met).
        """
        final_result: Optional[Dict[str, Any]] = None
        for event in self.iter_evolution(
            generations=generations,
            alpha_quality_weight=alpha_quality_weight,
            eval_cases=eval_cases,
            candidates_per_generation=candidates_per_generation,
            auto_apply_min_gain=auto_apply_min_gain,
        ):
            if event.get("type") == "complete":
                final_result = event["result"]
        assert final_result is not None
        return final_result
