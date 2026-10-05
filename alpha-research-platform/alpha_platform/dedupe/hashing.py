"""
Canonical hashing for alpha_config dedup, matching the pattern from the
"Avoid Duplicate Simulation" doc: sha256 of the sorted-key JSON of the
simulation_data dict exactly as it would be POSTed to /simulations.

This must stay byte-for-byte consistent with alpha_config.config_hash's
computation — if this changes, existing rows become unfindable by hash
and effectively "forgotten" by the dedupe check.
"""
import hashlib
import json


def hash_alpha(simulate_data: dict) -> str:
    """
    sha256 of the simulate_data dict, sorted-key JSON serialized, exactly
    as ace_lib.generate_alpha() returns it (before submission). Same
    algorithm as the user's original parquet-cache hash_alpha(), now the
    canonical version used for alpha_config.config_hash.
    """
    alpha_str = json.dumps(simulate_data, sort_keys=True)
    return hashlib.sha256(alpha_str.encode("utf-8")).hexdigest()