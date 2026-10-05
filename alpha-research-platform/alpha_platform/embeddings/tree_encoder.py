"""
Deterministic structural embedding for alpha expressions, derived from
alpha_structure (operators, data_fields, depth, complexity, etc) --
NOT a trained neural encoder, and NOT weight-tuned by hand. At current
KB size there isn't enough (expression, outcome) data to calibrate
weights against real ground truth, so this version uses NEUTRAL
(unweighted) sub-vectors rather than hand-guessed weights that would
falsely imply precision that doesn't exist.

Uses the hashing trick for the operator and field bags: each name is
hashed into a fixed bucket range rather than requiring a fixed
vocabulary, so new operators/fields never break or require retraining.

WHAT THIS EMBEDDING IS RELIABLE FOR:
  - Exact structural matches (ast_hash parameter sweeps) -> cosine 1.0.
    This is the one property verified against ground truth (ast_hash
    equality), and the main thing dedup/novelty search actually needs.
  - Rough clustering: "more similar" vs "less similar" in a directional
    sense.

WHAT THIS EMBEDDING IS NOT:
  - A calibrated similarity score. Absolute cosine values between
    non-identical expressions should NOT be trusted for fine-grained
    thresholds (e.g. "anything above 0.8 is basically a duplicate")
    without calibration -- see calibration path below.

CALIBRATION PATH (not yet runnable -- needs real data):
  Once the KB has enough alphas with self_correlation populated
  (requires check_self_corr=True on simulation), a future
  embeddings/calibration.py should check whether embedding cosine-
  distance actually correlates with BRAIN's own self_correlation
  numbers across real alpha pairs, and fit sub-vector weights against
  that real signal instead of guessing. Until then, this file uses
  equal (1.0) weights for every sub-vector rather than pretending
  tuned precision.

DESIGNED TO BE SWAPPED LATER: encode() has a fixed signature and output
shape (a flat float vector of length EMBEDDING_DIM). Once there's
enough labeled data to train a real tree encoder (GNN or tree-LSTM over
ast_json), swap the implementation behind this same function and bump
MODEL_VERSION -- callers (embeddings/pipeline.py, Qdrant writes) don't
need to change.
"""
from __future__ import annotations

import hashlib
import math

MODEL_VERSION = "structural-hash-v1"

_OPERATOR_BUCKETS = 64
_FIELD_BUCKETS = 64
_SCALAR_FEATURES = 6
_OP_TYPE_BUCKETS = 4

EMBEDDING_DIM = _OPERATOR_BUCKETS + _FIELD_BUCKETS + _SCALAR_FEATURES + _OP_TYPE_BUCKETS

# All sub-vectors get equal weight (1.0) -- deliberately NOT hand-tuned.
# See the calibration path in the module docstring for how these
# should eventually be set from real data.
_OPERATOR_WEIGHT = 1.0
_FIELD_WEIGHT = 1.0
_SCALAR_WEIGHT = 1.0
_OP_TYPE_WEIGHT = 1.0

_ARITHMETIC_OPS = {"+", "-", "*", "/", "unary-"}
_COMPARISON_OPS = {">", "<", ">=", "<=", "==", "!="}
_BOOLEAN_OPS = {"&&", "||"}


def _hash_bucket(name: str, num_buckets: int, salt: str) -> int:
    h = hashlib.sha256(f"{salt}:{name}".encode("utf-8")).hexdigest()
    return int(h, 16) % num_buckets


def _normalize(value: float, scale: float) -> float:
    """Squashes an unbounded non-negative count/score into [0, 1) via value/(value+scale)."""
    return value / (value + scale) if value >= 0 else 0.0


def _l2_normalize_slice(vec: list[float], offset: int, length: int) -> None:
    norm = math.sqrt(sum(v * v for v in vec[offset:offset + length]))
    if norm > 0:
        for i in range(offset, offset + length):
            vec[i] /= norm


def encode(
    *,
    operators: list[str],
    data_fields: list[str],
    expression_depth: int,
    expression_node_count: int,
    operator_count: int,
    unique_operator_count: int,
    field_count: int,
    complexity_score: float,
) -> list[float]:
    """
    Produces a flat EMBEDDING_DIM-length vector from alpha_structure
    fields. Pure function -- no DB/network access. Deterministic: same
    inputs always produce the same output.

    Each of the four sub-vectors (operator bag, field bag, scalars,
    op-type histogram) is L2-normalized independently, then scaled by
    its weight constant (currently all 1.0 -- see module docstring).
    """
    vec = [0.0] * EMBEDDING_DIM

    op_offset = 0
    for op in operators:
        bucket = _hash_bucket(op, _OPERATOR_BUCKETS, salt="operator")
        vec[op_offset + bucket] += 1.0
    _l2_normalize_slice(vec, op_offset, _OPERATOR_BUCKETS)
    for i in range(op_offset, op_offset + _OPERATOR_BUCKETS):
        vec[i] *= _OPERATOR_WEIGHT

    field_offset = _OPERATOR_BUCKETS
    for f in data_fields:
        bucket = _hash_bucket(f, _FIELD_BUCKETS, salt="field")
        vec[field_offset + bucket] += 1.0
    _l2_normalize_slice(vec, field_offset, _FIELD_BUCKETS)
    for i in range(field_offset, field_offset + _FIELD_BUCKETS):
        vec[i] *= _FIELD_WEIGHT

    scalar_offset = _OPERATOR_BUCKETS + _FIELD_BUCKETS
    vec[scalar_offset + 0] = _normalize(expression_depth, scale=5.0)
    vec[scalar_offset + 1] = _normalize(expression_node_count, scale=10.0)
    vec[scalar_offset + 2] = _normalize(operator_count, scale=5.0)
    vec[scalar_offset + 3] = _normalize(unique_operator_count, scale=5.0)
    vec[scalar_offset + 4] = _normalize(field_count, scale=3.0)
    vec[scalar_offset + 5] = _normalize(complexity_score, scale=50.0)
    _l2_normalize_slice(vec, scalar_offset, _SCALAR_FEATURES)
    for i in range(scalar_offset, scalar_offset + _SCALAR_FEATURES):
        vec[i] *= _SCALAR_WEIGHT

    op_type_offset = scalar_offset + _SCALAR_FEATURES
    arithmetic_n = sum(1 for op in operators if op in _ARITHMETIC_OPS)
    comparison_n = sum(1 for op in operators if op in _COMPARISON_OPS)
    boolean_n = sum(1 for op in operators if op in _BOOLEAN_OPS)
    call_n = len(operators) - arithmetic_n - comparison_n - boolean_n
    total_ops = max(len(operators), 1)
    vec[op_type_offset + 0] = arithmetic_n / total_ops
    vec[op_type_offset + 1] = comparison_n / total_ops
    vec[op_type_offset + 2] = boolean_n / total_ops
    vec[op_type_offset + 3] = call_n / total_ops
    _l2_normalize_slice(vec, op_type_offset, _OP_TYPE_BUCKETS)
    for i in range(op_type_offset, op_type_offset + _OP_TYPE_BUCKETS):
        vec[i] *= _OP_TYPE_WEIGHT

    return vec