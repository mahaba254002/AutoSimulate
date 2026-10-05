"""Normalize scoped catalogue records while retaining the original metadata."""
import math


def label(value):
    if isinstance(value, dict):
        return value.get('id') or value.get('name'), value.get('name') or value.get('id')
    return value, value


def coverage(value):
    try:
        number = float(value)
        return round(number * 100, 4) if math.isfinite(number) and 0 <= number <= 1 else None
    except (TypeError, ValueError):
        return None


def dataset_record(payload):
    category_id, category_name = label(payload.get('category'))
    return dict(dataset_id=payload['id'], name=payload.get('name') or payload['id'],
                category_id=category_id, category_name=category_name,
                instrument_coverage=coverage(payload.get('coverage')), date_coverage=coverage(payload.get('dateCoverage')),
                search_text=' '.join(str(payload.get(k) or '') for k in ('id','name','description','category')).lower(),
                payload=payload)


def field_record(payload, datasets):
    dataset_id, _ = label(payload.get('dataset') or payload.get('datasetId'))
    dataset = datasets.get(dataset_id, {})
    category_id, category_name = label(dataset.get('category') or payload.get('category'))
    return dict(field_id=payload['id'], dataset_id=dataset_id, dataset_name=dataset.get('name'),
                category_id=category_id, category_name=category_name, field_type=payload.get('type'),
                instrument_coverage=coverage(payload.get('coverage')), date_coverage=coverage(payload.get('dateCoverage')),
                search_text=' '.join(str(v or '') for v in (payload.get('id'),payload.get('name'),payload.get('description'),dataset_id,dataset.get('name'),category_name)).lower(),
                payload=payload)
