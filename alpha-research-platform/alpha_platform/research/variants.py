"""Auditable catalogue-based field substitutions; no provider or network calls."""
from copy import deepcopy
from itertools import product
import re

from alpha_platform.generation.gp.trees import walk
from alpha_platform.structure.parser import Identifier, parse
from alpha_platform.structure.serializer import serialize
from alpha_platform.research.planning import field_dataset
from alpha_platform.research.catalog import coverage_percent

STOP_WORDS = set("the and for with from this that data field company companies financial value values reported measure measures based each per total annual quarterly current historical calculated number adjusted".split())


def terms(field):
    return set(re.findall(r"[a-z]{3,}", str(field.get("description", "")).lower())) - STOP_WORDS


def category(dataset):
    value = dataset.get("category")
    if isinstance(value, dict):
        return str(value.get("id") or value.get("name") or "").lower()
    return str(value or "").lower()


def field_variants(expression, fields, datasets, count, seed):
    tree = parse(expression)
    originals = sorted({n.name for _, n in walk(tree) if isinstance(n, Identifier) and n.name in fields})
    if not 1 <= len(originals) <= 3:
        raise ValueError("Template variants support one to three distinct documented scalar fields.")
    datasets = {d["id"]: d for d in datasets}
    slots, pools = [], []
    for original in originals:
        source = fields[original]
        source_category = category(datasets.get(field_dataset(source), {}))
        if not source_category:
            raise ValueError(f"Cannot identify the dataset category for {original}. Sync its dataset metadata first.")
        source_terms = terms(source)
        choices = []
        for name, candidate in fields.items():
            if name in originals or category(datasets.get(field_dataset(candidate), {})) != source_category:
                continue
            # Known units and frequency must agree. Missing metadata stays explicit.
            if any(source.get(key) and candidate.get(key) and str(source[key]).lower() != str(candidate[key]).lower()
                   for key in ("units", "frequency")):
                continue
            candidate_terms = terms(candidate)
            shared = source_terms & candidate_terms
            score = len(shared) / max(1, len(source_terms | candidate_terms))
            if len(shared) < 2 or score < .25:
                continue
            coverage = coverage_percent(candidate, "coverage")
            date_coverage = coverage_percent(candidate, "dateCoverage")
            if coverage is None or date_coverage is None or coverage <= 0 or date_coverage <= 0:
                continue
            choices.append({"field_id": name, "dataset_id": field_dataset(candidate),
                            "description": candidate["description"], "similarity": round(score, 4),
                            "shared_terms": sorted(shared),
                            "instrument_coverage": coverage, "date_coverage": date_coverage,
                            "missing_metadata": [key for key in ("units", "frequency") if not source.get(key) or not candidate.get(key)]})
        choices.sort(key=lambda row: (-row["instrument_coverage"], -row["date_coverage"], -row["similarity"], row["field_id"]))
        choices = choices[:50]
        if not choices:
            raise ValueError(f"No similar replacement fields found for {original} in category {source_category}. "
                             "Sync more documented fields in the same market, universe and delay, or use manual expressions.")
        slots.append({"original_field": original, "dataset_id": field_dataset(source),
                      "category": source_category, "description": source["description"], "choices": choices})
        pools.append([choice["field_id"] for choice in choices])
    combinations = [combo for combo in product(*pools) if len(set(combo)) == len(combo)]
    ranks = [{row["field_id"]: row for row in slot["choices"]} for slot in slots]
    def quality(combo):
        rows = [ranks[index][name] for index, name in enumerate(combo)]
        return (-min(row["instrument_coverage"] for row in rows),
                -sum(row["instrument_coverage"] for row in rows),
                -sum(row["date_coverage"] for row in rows),
                -sum(row["similarity"] for row in rows), combo)
    combinations.sort(key=quality)
    rows = []
    for combo in combinations[:count]:
        mapping = dict(zip(originals, combo))
        variant = deepcopy(tree)
        for _, node in walk(variant):
            if isinstance(node, Identifier) and node.name in mapping:
                node.name = mapping[node.name]
        rows.append({"expression": serialize(variant), "template": "Field substitution template",
                     "rationale": "Catalogue description similarity; substitutions require user review. " +
                                  "; ".join(f"{key} → {value}" for key, value in mapping.items()),
                     "substitutions": mapping})
    if not rows:
        raise ValueError("No distinct compatible field combinations are available for this template.")
    return rows, {"source_expression": expression, "requested": count, "generated": len(rows),
                  "slots": slots,
                  "method": "Same documented category and MATRIX type; matching known units/frequency; description word overlap ≥25% and two shared terms. Positive documented instrument and date coverage required. Up to 50 replacements per field, ranked by instrument coverage, date coverage, then description similarity. Combinations ranked by weakest instrument coverage, total coverage, date coverage, then similarity. No random selection."}
