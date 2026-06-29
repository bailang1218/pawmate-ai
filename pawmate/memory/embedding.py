from __future__ import annotations

import hashlib
import math
import re


class HashingEmbeddingProvider:
    """Small local embedding provider used as an offline vector baseline.

    It is deterministic, dependency-free, and good enough to support hybrid
    recall while leaving a clean provider boundary for real embedding models.
    """

    model_name = "pawmate-hashing-v1"

    def __init__(self, dim: int = 128) -> None:
        self.dim = max(16, int(dim))

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            return vector
        return [value / norm for value in vector]

    @staticmethod
    def _tokens(text: str) -> list[str]:
        raw = (text or "").lower()
        tokens = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]", raw)
        compact = re.sub(r"\s+", "", raw)
        if len(compact) >= 3:
            tokens.extend(compact[i : i + 3] for i in range(len(compact) - 2))
        return tokens


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))
