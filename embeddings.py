import hashlib
import math
import re
from array import array

EMBEDDING_MODEL = "hashed-ngram-v1"
EMBEDDING_DIM = 256

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def content_fingerprint(text: str) -> str:
    """Stable hash of embedding input, used to skip recomputation."""
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()


def _features(text: str):
    tokens = _TOKEN_RE.findall((text or "").casefold())
    for token in tokens:
        yield "w:" + token
        if len(token) >= 4:
            for i in range(len(token) - 2):
                yield "c:" + token[i : i + 3]
    for first, second in zip(tokens, tokens[1:]):
        yield "b:" + first + "_" + second


def embed_text(text: str) -> bytes:
    """Return an EMBEDDING_DIM-float32, L2-normalized vector as raw bytes."""
    vector = [0.0] * EMBEDDING_DIM
    for feature in _features(text):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [value / norm for value in vector]
    return array("f", vector).tobytes()


def cosine_similarity(vector_a: bytes, vector_b: bytes) -> float:
    """Dot product of two vectors from embed_text, valid since both are
    already L2-normalized, so this equals cosine similarity."""
    a = array("f")
    a.frombytes(vector_a)
    b = array("f")
    b.frombytes(vector_b)
    return sum(x * y for x, y in zip(a, b))
