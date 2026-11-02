"""임베딩 (앱 설계 §7). 모델마다 embed(texts, kind)만 맞춘다 — kind는 document 또는 query.
FakeEmbedder는 낱말 해시 벡터라 키 없이 테스트·fixture가 돈다."""

import hashlib
import re

import numpy as np

BATCH = 100


class FakeEmbedder:
    def __init__(self, model: str = "fake", dim: int = 8):
        self.model, self.dim = model, dim
        self.calls = self.texts = 0

    def embed(self, texts: list[str], kind: str) -> list[np.ndarray]:
        self.calls += 1
        self.texts += len(texts)
        out = []
        for text in texts:
            v = np.ones(self.dim, dtype="float32") * 1e-3
            for word in re.findall(r"\w+", text):
                digest = hashlib.sha256(word.encode()).digest()
                v[digest[0] % self.dim] += 1.0
            out.append(v)
        return out


class OpenAIEmbedder:
    def __init__(self, model: str):
        from openai import OpenAI  # noqa: PLC0415 — 키 없는 테스트가 SDK를 띄우지 않게

        self.model, self.client = model, OpenAI()

    def embed(self, texts: list[str], kind: str) -> list[np.ndarray]:
        data = self.client.embeddings.create(model=self.model, input=texts).data
        return [np.array(d.embedding, dtype="float32") for d in data]


class VoyageEmbedder:
    def __init__(self, model: str):
        import voyageai  # noqa: PLC0415

        self.model, self.client = model, voyageai.Client()

    def embed(self, texts: list[str], kind: str) -> list[np.ndarray]:
        result = self.client.embed(texts, model=self.model, input_type=kind)
        return [np.array(v, dtype="float32") for v in result.embeddings]


def make_embedder(model: str):
    return VoyageEmbedder(model) if model.startswith("voyage") else OpenAIEmbedder(model)
