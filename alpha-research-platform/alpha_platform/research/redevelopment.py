"""Bounded, documented structural experiments; no provider or simulation calls."""
from copy import deepcopy
from alpha_platform.structure.parser import parse, FunctionCall, Identifier, NumberLiteral, KeywordArg, BinaryOp
from alpha_platform.structure.serializer import serialize
from alpha_platform.generation.gp.trees import walk, replace_at


def redevelopment_plan(expression, fields, windows, limit, available_ops=None):
    tree = parse(expression)
    known = {f['id']: f for f in fields}
    used = sorted({n.name for _, n in walk(tree) if isinstance(n, Identifier) and n.name in known})
    if not used:
        raise ValueError('The parent must use documented fields from the selected scope.')
    # Preserve explicit group neutralization in every component experiment.
    grouped = isinstance(tree, FunctionCall) and tree.operator == 'group_neutralize'
    signal = tree.args[0] if grouped else tree
    def wrap(node):
        if not grouped: return node
        result = deepcopy(tree)
        result.args[0] = node
        return result
    def components(node):
        if isinstance(node, BinaryOp) and node.op == '+':
            return components(node.left) + components(node.right)
        if isinstance(node, FunctionCall) and node.operator == 'add' and not any(isinstance(a, KeywordArg) for a in node.args):
            return [c for a in node.args for c in components(a)]
        return [node]
    parts = components(signal)
    rows, seen, excluded = [], {serialize(tree)}, []
    def emit(node, name, hypothesis, phase, change, family=None):
        required = {n.operator for _, n in walk(node) if isinstance(n, FunctionCall)}
        if available_ops is not None and not required.issubset(available_ops):
            excluded.append({'branch':name, 'unavailable_operators':sorted(required - available_ops)})
            return
        canonical = serialize(node)
        if canonical in seen: return
        seen.add(canonical)
        rows.append({'expression':canonical, 'template':name, 'rationale':hypothesis,
                     'phase':phase, 'change':change, 'family':family or name})
    for i, part in enumerate(parts):
        emit(wrap(deepcopy(part)), f'Component {i+1}',
             'Diagnostic: test this parent component alone, preserving explicit group neutralization and simulation settings.',
             0, 'Isolate one additive component.')
    base_window = windows[0]
    # Change one occurrence at a time; all other parent components stay intact.
    for index, (path, node) in enumerate(walk(tree)):
        if not isinstance(node, FunctionCall) or node.operator != 'rank' or len(node.args) != 1: continue
        field_names = sorted({n.name for _, n in walk(node.args[0]) if isinstance(n, Identifier) and n.name in known})
        if len(field_names) != 1: continue
        field_id = field_names[0]
        for kind in ('slope', 'residual', 'historical surprise'):
            family = f'{kind.title()} · component {index}'
            for w in windows:
                if kind == 'historical surprise':
                    transformed = FunctionCall('ts_zscore', [Identifier(field_id), NumberLiteral(w)])
                    meaning = 'a reading above its own rolling mean, scaled by its rolling standard deviation'
                else:
                    ret = 2 if kind == 'slope' else 0
                    transformed = FunctionCall('ts_regression', [Identifier(field_id), FunctionCall('ts_step',[NumberLiteral(1)]),
                                               NumberLiteral(w), KeywordArg('rettype',NumberLiteral(ret))])
                    meaning = 'the fitted time-trend slope (rettype=2)' if ret == 2 else 'the error term relative to the fitted time trend (rettype=0)'
                revised = replace_at(tree, path, FunctionCall('rank',[transformed]))
                description = known[field_id].get('description','')
                hypothesis = (f'Test whether {meaning} for {field_id} contributes more useful information than the parent component. '
                              f'Window: {w} trading days. Documented field: {description} '
                              'Positive exposure preserves the parent direction; its economic interpretation requires review.')
                emit(revised, f'{family} · {w} days', hypothesis, 1 if w == base_window else 2,
                     f'Replace only rank occurrence {index} with ranked {kind}; other components and groups are preserved.', family)
    if len(parts) == 2 and all(isinstance(p, FunctionCall) and p.operator == 'rank' for p in parts):
        agreement = FunctionCall('min',deepcopy(parts))
        emit(wrap(agreement), 'Joint high ranks',
             'Test whether rewarding the weaker of two ranked components better captures joint strength than their additive sum. '
             'This is a graded agreement score, not a Boolean trading gate.', 1, 'Replace the sum of two ranks with their minimum.')
    rows.sort(key=lambda r:r['phase'])
    if not rows: raise ValueError('No supported redevelopment branches were found. Use a ranked signal or an additive ranked expression.')
    rows = rows[:min(limit, 200)]
    if not any(r['phase'] == 1 for r in rows):
        raise ValueError('No controlled revision fits the available operators and attempt limit. Increase the attempt limit or select a scope supporting regression or historical standardization.')
    return rows, {'parent_expression':serialize(tree), 'windows':windows, 'branches':rows,
                 'excluded_branches':excluded,
                 'method':'Component diagnostics first; controlled revisions second; nearby windows only for positive-Sharpe, positive-fitness branches within the turnover threshold. No new fields or economic meanings are invented.',
                 'unavailable':['Parent return correlation and independent period tests require observations not available in this catalogue.'],
                 'field_evidence':[known[name] for name in used]}


def parent_comparison(metrics, parent):
    baseline = (parent or {}).get('metrics', {})
    return {'parent_run_id':(parent or {}).get('run_id'), 'parent_metrics':baseline,
            'delta':{k:metrics[k]-baseline[k] if metrics.get(k) is not None and baseline.get(k) is not None else None
                     for k in ('sharpe','fitness','turnover')},
            'parent_correlation':None, 'note':'Parent return correlation has not been measured. Metric differences are not independent statistical evidence.'}
