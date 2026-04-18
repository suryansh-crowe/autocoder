"""LLM stage — single, low-token call to Phi-4 (Ollama).

Design rules — see info/06_optimized_architecture.md:

* The LLM never writes Python. It returns a tiny JSON action plan.
* Every plan is grammar-validated against the extracted catalog
  *before* it reaches the renderer, so hallucinated method names or
  unknown elements are rejected up-front.
* All plans are cached on disk (``manifest/plans/``) keyed by the
  extraction fingerprint, so reruns are free unless the page changed.
"""

from autocoder.llm.ollama_client import OllamaClient
from autocoder.llm.plans import generate_feature_plan, generate_pom_plan

__all__ = ["OllamaClient", "generate_pom_plan", "generate_feature_plan"]
