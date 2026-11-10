"""Enterprise Memory Studio package: 4-Scope Evaluation Engine & AlphaEvolve Auto-Tuner."""

from src.studio.alpha_evolve import (
    INJECTION_FORMATS,
    TOKEN_NICHES,
    VECTOR_NICHES,
    AlphaEvolveEngine,
    MAPElitesArchive,
    MemoryConfig,
)
from src.studio.eval_engine import (
    EVAL_ARCHETYPES,
    EvalCase,
    EvalEngine,
)

__all__ = [
    "EVAL_ARCHETYPES",
    "INJECTION_FORMATS",
    "TOKEN_NICHES",
    "VECTOR_NICHES",
    "AlphaEvolveEngine",
    "EvalCase",
    "EvalEngine",
    "MAPElitesArchive",
    "MemoryConfig",
]
