from __future__ import annotations


class _DisabledRuntimeModel:
    def __init__(self):
        self.model_loaded = False
        self.metadata = {"model_version": "disabled-local-compat"}


class AIModel:
    """Compatibilidade minima para o runtime atual sem IA ativa."""

    def __init__(self):
        self.runtime_model = _DisabledRuntimeModel()

    def get_runtime_status(self):
        return {
            "enabled": False,
            "model_version": self.runtime_model.metadata.get("model_version"),
            "runtime_version": self.runtime_model.metadata.get("model_version"),
            "reason": "AI runtime indisponivel neste workspace.",
        }
