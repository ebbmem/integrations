"""Filter evaluation and namespace matching helpers matching LangGraph BaseStore semantics."""

from __future__ import annotations

from typing import Any

from langgraph.store.base import ListNamespacesOp, MatchCondition


def compare_values(item_value: Any, filter_value: Any) -> bool:
    """Compare stored item field value against a LangGraph JSONB-style filter."""
    if isinstance(filter_value, dict):
        if any(k.startswith("$") for k in filter_value):
            return all(
                apply_operator(item_value, op_key, op_value)
                for op_key, op_value in filter_value.items()
            )
        if not isinstance(item_value, dict):
            return False
        return all(compare_values(item_value.get(k), v) for k, v in filter_value.items())
    if isinstance(filter_value, list | tuple):
        return (
            isinstance(item_value, list | tuple)
            and len(item_value) == len(filter_value)
            and all(
                compare_values(iv, fv) for iv, fv in zip(item_value, filter_value, strict=False)
            )
        )
    return bool(item_value == filter_value)


def apply_operator(value: Any, operator: str, op_value: Any) -> bool:
    """Evaluate a comparison operator (`$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`)."""
    if operator == "$eq":
        return bool(value == op_value)
    if operator == "$ne":
        return bool(value != op_value)
    if operator in {"$gt", "$gte", "$lt", "$lte"}:
        if value is None or isinstance(value, bool) or isinstance(op_value, bool):
            return False
        try:
            lhs = float(value)
            rhs = float(op_value)
        except (TypeError, ValueError):
            return False
        if operator == "$gt":
            return lhs > rhs
        if operator == "$gte":
            return lhs >= rhs
        if operator == "$lt":
            return lhs < rhs
        return lhs <= rhs
    raise ValueError(f"Unsupported filter operator: {operator}")


def matches_filter(value: dict[str, Any], filter_spec: dict[str, Any] | None) -> bool:
    """Return True if stored item dict matches all conditions in `filter_spec`."""
    if not filter_spec:
        return True
    return all(compare_values(value.get(k), v) for k, v in filter_spec.items())


def does_match_namespace(condition: MatchCondition, namespace: tuple[str, ...]) -> bool:
    """Evaluate a LangGraph `MatchCondition` (prefix/suffix with `*` wildcards)."""
    path = condition.path
    if len(namespace) < len(path):
        return False
    if condition.match_type == "prefix":
        for ns_elem, p_elem in zip(namespace, path, strict=False):
            if p_elem != "*" and ns_elem != p_elem:
                return False
        return True
    if condition.match_type == "suffix":
        for ns_elem, p_elem in zip(reversed(namespace), reversed(path), strict=False):
            if p_elem != "*" and ns_elem != p_elem:
                return False
        return True
    raise ValueError(f"Unsupported match type: {condition.match_type}")


def filter_and_sort_namespaces(
    namespaces: set[tuple[str, ...]],
    op: ListNamespacesOp,
) -> list[tuple[str, ...]]:
    """Apply MatchConditions, `max_depth` truncation, sorting, and offset/limit slicing."""
    matched = list(namespaces)
    if op.match_conditions:
        matched = [
            ns
            for ns in matched
            if all(does_match_namespace(cond, ns) for cond in op.match_conditions)
        ]
    if op.max_depth is not None:
        if op.max_depth <= 0:
            sorted_ns = [()] if matched else []
        else:
            sorted_ns = sorted({ns[: op.max_depth] for ns in matched})
    else:
        sorted_ns = sorted(set(matched))
    if op.limit <= 0:
        return []
    start = max(0, op.offset)
    return sorted_ns[start : start + op.limit]
