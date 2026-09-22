"""Real semantic embedding provider boundary.

PawMate deliberately does not fall back to feature hashing. If no real model
is configured, semantic retrieval is reported as disabled and the memory
system uses SQLite FTS5 plus layered conversation retrieval instead.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen


class EmbeddingProvider(Protocol):
    model_name: str
    enabled: bool
    reason: str

    def embed(self, text: str) -> list[float]: ...


class DisabledEmbeddingProvider:
    model_name = "disabled"
    enabled = False

    def __init__(self, reason: str = "no_real_embedding_model_configured") -> None:
        self.reason = reason

    def embed(self, text: str) -> list[float]:
        return []


class OnnxEmbeddingProvider:
    """Run a local sentence embedding ONNX model with a tokenizer.json.

    Configure with ``PAWMATE_EMBEDDING_MODEL_DIR``. The directory must contain
    ``model.onnx`` and ``tokenizer.json``. Models that return either a pooled
    2-D vector or token-level 3-D hidden states are supported.
    """

    enabled = True
    reason = "configured_local_onnx_model"

    def __init__(self, model_dir: Path, max_length: int = 256) -> None:
        from tokenizers import Tokenizer
        import onnxruntime as ort

        root = Path(model_dir).expanduser().resolve()
        model_path = root / "model.onnx"
        tokenizer_path = root / "tokenizer.json"
        if not model_path.is_file() or not tokenizer_path.is_file():
            raise FileNotFoundError(
                "embedding model directory must contain model.onnx and tokenizer.json"
            )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=max(8, int(max_length)))
        self._session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        self.model_name = os.environ.get(
            "PAWMATE_EMBEDDING_MODEL_NAME",
            f"onnx:{root.name}",
        )

    def embed(self, text: str) -> list[float]:
        import numpy as np

        encoded = self._tokenizer.encode(str(text or ""))
        ids = np.asarray([encoded.ids], dtype=np.int64)
        mask_values = encoded.attention_mask or [1] * len(encoded.ids)
        mask = np.asarray([mask_values], dtype=np.int64)
        type_ids = np.asarray([encoded.type_ids or [0] * len(encoded.ids)], dtype=np.int64)
        feed = {}
        for model_input in self._session.get_inputs():
            name = model_input.name
            lowered = name.lower()
            if "attention" in lowered:
                feed[name] = mask
            elif "token_type" in lowered or "segment" in lowered:
                feed[name] = type_ids
            else:
                feed[name] = ids
        output = self._session.run(None, feed)[0]
        if output.ndim == 3:
            weights = mask.astype(np.float32)[..., None]
            pooled = (output * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
            vector = pooled[0]
        elif output.ndim == 2:
            vector = output[0]
        else:
            vector = output.reshape(-1)
        return _normalize([float(value) for value in vector.tolist()])


class HttpEmbeddingProvider:
    """Call a configured embedding service using the common /embeddings shape."""

    enabled = True
    reason = "configured_http_embedding_service"

    def __init__(self, endpoint: str, model_name: str, api_key: str = "") -> None:
        endpoint = str(endpoint or "").strip()
        model_name = str(model_name or "").strip()
        if not endpoint or not model_name:
            raise ValueError("embedding endpoint and model are required")
        self._endpoint = endpoint
        self._api_key = api_key
        self.model_name = f"http:{model_name}"
        self._request_model = model_name

    def embed(self, text: str) -> list[float]:
        payload = json.dumps(
            {"model": self._request_model, "input": [str(text or "")]},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(self._endpoint, data=payload, headers=headers, method="POST")
        timeout = max(1.0, float(os.environ.get("PAWMATE_EMBEDDING_TIMEOUT", "15")))
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        vector = None
        if isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
            vector = data["data"][0].get("embedding")
        if vector is None and isinstance(data, dict):
            vector = data.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("embedding service returned no vector")
        return _normalize([float(value) for value in vector])


def create_embedding_provider() -> EmbeddingProvider:
    backend = os.environ.get("PAWMATE_EMBEDDING_BACKEND", "").strip().lower()
    model_dir = os.environ.get("PAWMATE_EMBEDDING_MODEL_DIR", "").strip()
    endpoint = os.environ.get("PAWMATE_EMBEDDING_ENDPOINT", "").strip()
    try:
        if backend == "onnx" or (not backend and model_dir):
            return OnnxEmbeddingProvider(Path(model_dir))
        if backend == "http" or (not backend and endpoint):
            return HttpEmbeddingProvider(
                endpoint,
                os.environ.get("PAWMATE_EMBEDDING_MODEL", "").strip(),
                os.environ.get("PAWMATE_EMBEDDING_API_KEY", "").strip(),
            )
    except Exception as exc:
        return DisabledEmbeddingProvider(f"embedding_provider_initialization_failed:{exc}")
    return DisabledEmbeddingProvider()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


# Import compatibility only. New code never instantiates this name.
HashingEmbeddingProvider = DisabledEmbeddingProvider
