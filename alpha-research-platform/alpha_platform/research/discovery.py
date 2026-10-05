"""Targeted metadata discovery for user-supplied templates; never simulations."""
import hashlib
from types import SimpleNamespace

from alpha_platform.generation.gp.trees import signal_slots
from alpha_platform.structure.parser import Identifier, parse
from alpha_platform.research.planning import field_dataset
from alpha_platform.research.catalog import ResearchSession, fetch_pages, read_json, CHECKPOINTS


def complete_pages(session, path, params, checkpoint):
    rows = fetch_pages(session, path, params, checkpoint)
    # Completed searches must be fresh on the next preparation, including misses.
    # Exceptions leave incomplete page checkpoints available for a later retry.
    checkpoint.unlink(missing_ok=True)
    checkpoint.with_suffix(".progress.json").unlink(missing_ok=True)
    return rows


def template_fields(expression):
    if len(expression) > 2000:
        raise ValueError("Template exceeds 2,000 characters.")
    try:
        identifiers = sorted({node.name for _, node in signal_slots(parse(expression)) if isinstance(node, Identifier)})
    except KeyError as error:
        raise ValueError(f"Unknown template operator: {error.args[0]}") from error
    return identifiers


def resolve_template_scope(scope, expression, include_replacements=True):
    identifiers = template_fields(expression)
    if include_replacements and not 1 <= len(identifiers) <= 3:
        raise ValueError("Template variants require one to three distinct data fields.")
    params = {key: scope.settings[key] for key in ("instrumentType", "region", "universe", "delay")}
    fields = {field["id"]: field for field in scope.fields}
    datasets = {dataset["id"]: dataset for dataset in scope.datasets}
    key = hashlib.sha256(scope.scope_id.encode()).hexdigest()[:16]
    fetched = []
    # Persistent page checkpoints permit a later user retry after a rate-limit response.
    with ResearchSession() as session:
        for identifier in identifiers:
            if identifier not in fields:
                search_key = hashlib.sha256(identifier.encode()).hexdigest()[:16]
                checkpoint = CHECKPOINTS / f"template-{key}-search-{search_key}.jsonl"
                # Search can rank rather than filter results. Return immediately when
                # the exact identifier appears; do not download the whole universe.
                import json
                matches = []
                if checkpoint.exists():
                    for line in checkpoint.read_text(encoding="utf-8").splitlines():
                        try:
                            matches.extend(json.loads(line)["results"])
                        except (ValueError, KeyError):
                            break
                if not any(row.get("id") == identifier for row in matches):
                    page = read_json(session, "/data-fields", {**params, "search": identifier, "limit":50, "offset":0})
                    matches = page.get("results")
                    if not isinstance(matches, list):
                        raise ValueError("BRAIN field search returned no valid results list.")
                exact = [row for row in matches if row["id"] == identifier]
                if len(exact) != 1:
                    raise ValueError(f"BRAIN did not return the exact field {identifier} for this region, universe and delay. "
                                     "No dataset or category was inferred from its name.")
                fields[identifier] = exact[0]
                checkpoint.unlink(missing_ok=True)
                checkpoint.with_suffix(".progress.json").unlink(missing_ok=True)
                fetched.append(identifier)
            source = fields[identifier]
            if str(source.get("type", "")).upper() != "MATRIX" or len(source.get("description") or "") < 10:
                raise ValueError(f"{identifier} lacks documented scalar metadata; replacements cannot be justified.")
        source_datasets = {field_dataset(fields[name]) for name in identifiers}
        if None in source_datasets or "" in source_datasets:
            raise ValueError("BRAIN field metadata did not identify its dataset.")
        if not source_datasets.issubset(datasets):
            checkpoint = CHECKPOINTS / f"template-{key}-datasets.jsonl"
            rows = complete_pages(session, "/data-sets", params, checkpoint)
            datasets.update({row["id"]: row for row in rows})
        if not source_datasets.issubset(datasets):
            raise ValueError("The source field's dataset could not be verified in this settings scope.")
        for dataset_id in sorted(source_datasets) if include_replacements else []:
            dataset_key = hashlib.sha256(dataset_id.encode()).hexdigest()[:16]
            checkpoint = CHECKPOINTS / f"template-{key}-fields-{dataset_key}.jsonl"
            rows = complete_pages(session, "/data-fields", {**params, "dataset.id": dataset_id}, checkpoint)
            returned = {row["id"]: row for row in rows}
            for identifier in identifiers:
                if field_dataset(fields[identifier]) == dataset_id and identifier not in returned:
                    raise ValueError(f"The exact source field {identifier} is absent from its scoped dataset listing. "
                                     "Research was not saved; no substitutions were guessed.")
            fields.update(returned)
    return SimpleNamespace(scope_id=scope.scope_id, settings=scope.settings, capabilities=scope.capabilities,
                           status=scope.status, synced_at=scope.synced_at,
                           fields=list(fields.values()), datasets=list(datasets.values())), {
        "resolved_missing_fields": fetched, "source_dataset_ids": sorted(source_datasets),
        "scanned_fields": len(fields), "settings": params,
        "note": ("Source datasets scanned completely to rank compatible fields globally by coverage. Only the source fields and up to 50 replacements per field are added to the saved catalogue."
                 if include_replacements else "Missing source field metadata resolved by exact scoped search. No replacement dataset scan or simulation was started.")}
