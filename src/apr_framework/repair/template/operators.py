"""AST mutation operator transformers for template-based APR.

Each operator targets only nodes whose line number matches `target_line`.
`generate_variants(tree)` returns a list of independently mutated AST copies.
"""

import ast
import copy
import sys
from abc import ABC, abstractmethod

from apr_framework.core.exceptions import ConfigurationError

if sys.version_info < (3, 9):
    raise ConfigurationError(
        "Template-based repair requires Python 3.9+ for ast.unparse() support. "
        f"Current version: {sys.version_info.major}.{sys.version_info.minor}"
    )


class _LineTargetedTransformer(ABC):
    """Base class for operators that restrict mutations to a single target line.

    Each concrete operator implements ``generate_variants(tree)``, returning one
    independently mutated *copy* of the AST per applicable substitution found on
    ``target_line``. Operators never mutate the input tree in place.
    """

    def __init__(self, target_line: int) -> None:
        self._target_line = target_line

    def _on_target_line(self, node: ast.AST) -> bool:
        lineno = getattr(node, "lineno", None)
        end_lineno = getattr(node, "end_lineno", lineno)
        if lineno is None:
            return False
        if end_lineno is None:
            end_lineno = lineno
        return lineno <= self._target_line <= end_lineno

    @abstractmethod
    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        """Return one mutated AST copy per applicable substitution at the target line."""
        ...


# ---------------------------------------------------------------------------
# arith — arithmetic operator replacement
# ---------------------------------------------------------------------------

_ARITH_SWAPS: list[tuple[type, type]] = [
    (ast.Add, ast.Sub),
    (ast.Sub, ast.Add),
    (ast.Mult, ast.Div),
    (ast.Div, ast.Mult),
    (ast.FloorDiv, ast.Mod),
    (ast.Mod, ast.FloorDiv),
]


class ArithmeticOperatorReplacer(_LineTargetedTransformer):
    """Swaps arithmetic operators in BinOp nodes on the target line."""

    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        """
        Generate multiple AST variants by swapping arithmetic/binary operators  one at a time
        """
        results: list[ast.AST] = []
        tree_copy = copy.deepcopy(tree)
        for node in ast.walk(tree_copy):
            if not isinstance(node, ast.BinOp):
                continue
            if not self._on_target_line(node):
                continue
            for original_cls, replacement_cls in _ARITH_SWAPS:
                if isinstance(node.op, original_cls):
                    variant = copy.deepcopy(tree_copy)
                    for variant_node in ast.walk(variant):
                        if (
                            isinstance(variant_node, ast.BinOp)
                            and isinstance(variant_node.op, original_cls)
                            and getattr(variant_node, "lineno", None)
                            == getattr(node, "lineno", None)
                            and getattr(variant_node, "col_offset", None)
                            == getattr(node, "col_offset", None)
                        ):
                            variant_node.op = replacement_cls()
                            break
                    ast.fix_missing_locations(variant)
                    results.append(variant)
        return results


# ---------------------------------------------------------------------------
# comp — comparison operator replacement
# ---------------------------------------------------------------------------

_COMP_SWAPS: list[tuple[type, type]] = [
    (ast.Gt, ast.GtE),
    (ast.GtE, ast.Gt),
    (ast.Lt, ast.LtE),
    (ast.LtE, ast.Lt),
    (ast.Eq, ast.NotEq),
    (ast.NotEq, ast.Eq),
    (ast.Is, ast.IsNot),
    (ast.IsNot, ast.Is),
    (ast.In, ast.NotIn),
    (ast.NotIn, ast.In),
]


class ComparisonOperatorReplacer(_LineTargetedTransformer):
    """Swaps comparison operators in Compare nodes on the target line."""

    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        """
        Generate multiple AST variants by swapping comparison operators  one at a time
        """
        results: list[ast.AST] = []
        tree_copy = copy.deepcopy(tree)
        for node in ast.walk(tree_copy):
            if not isinstance(node, ast.Compare):
                continue
            if not self._on_target_line(node):
                continue
            for op_idx, op in enumerate(node.ops):
                for original_cls, replacement_cls in _COMP_SWAPS:
                    if isinstance(op, original_cls):
                        variant = copy.deepcopy(tree_copy)
                        for variant_node in ast.walk(variant):
                            if (
                                isinstance(variant_node, ast.Compare)
                                and getattr(variant_node, "lineno", None)
                                == getattr(node, "lineno", None)
                                and getattr(variant_node, "col_offset", None)
                                == getattr(node, "col_offset", None)
                                and len(variant_node.ops) > op_idx
                                and isinstance(variant_node.ops[op_idx], original_cls)
                            ):
                                variant_node.ops[op_idx] = replacement_cls()
                                break
                        ast.fix_missing_locations(variant)
                        results.append(variant)
        return results


# ---------------------------------------------------------------------------
# obo — off-by-one replacement
# ---------------------------------------------------------------------------


class OffByOneReplacer(_LineTargetedTransformer):
    """Emits n+1 and n-1 variants for integer constants and range() upper bounds."""

    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        """
        Generate multiple AST variants by of by one changes (+1/-1)  one at a time
        """
        results: list[ast.AST] = []
        tree_copy = copy.deepcopy(tree)

        # Visit only target line nodes
        for node in ast.walk(tree_copy):
            if not self._on_target_line(node):
                continue

            # Integer Constant nodes
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                for delta in (+1, -1):
                    variant = copy.deepcopy(tree_copy)
                    for variant_node in ast.walk(variant):
                        if (
                            isinstance(variant_node, ast.Constant)
                            and isinstance(variant_node.value, int)
                            and getattr(variant_node, "lineno", None)
                            == getattr(node, "lineno", None)
                            and getattr(variant_node, "col_offset", None)
                            == getattr(node, "col_offset", None)
                        ):
                            variant_node.value = node.value + delta
                            break
                    ast.fix_missing_locations(variant)
                    results.append(variant)

            # range(n) upper bound: nudge the last positional argument
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "range"
                and node.args
                and not node.keywords
            ):
                last_arg = node.args[-1]
                if isinstance(last_arg, ast.Constant) and isinstance(
                    last_arg.value, int
                ):
                    for delta in (+1, -1):
                        variant = copy.deepcopy(tree_copy)
                        for variant_node in ast.walk(variant):
                            if (
                                isinstance(variant_node, ast.Call)
                                and isinstance(variant_node.func, ast.Name)
                                and variant_node.func.id == "range"
                                and variant_node.args
                                and getattr(variant_node, "lineno", None)
                                == getattr(node, "lineno", None)
                                and getattr(variant_node, "col_offset", None)
                                == getattr(node, "col_offset", None)
                            ):
                                variant_range_arg = variant_node.args[-1]
                                if isinstance(variant_range_arg, ast.Constant):
                                    variant_range_arg.value = last_arg.value + delta
                                break
                        ast.fix_missing_locations(variant)
                        results.append(variant)

        return results


# ---------------------------------------------------------------------------
# bool — boolean operator replacement
# ---------------------------------------------------------------------------


class BooleanOperatorReplacer(_LineTargetedTransformer):
    """Swaps And<->Or in BoolOp nodes on the target line."""

    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        results: list[ast.AST] = []
        tree_copy = copy.deepcopy(tree)
        for node in ast.walk(tree_copy):
            if not isinstance(node, ast.BoolOp):
                continue
            if not self._on_target_line(node):
                continue
            replacement_op_cls = ast.Or if isinstance(node.op, ast.And) else ast.And
            variant = copy.deepcopy(tree_copy)
            for variant_node in ast.walk(variant):
                if (
                    isinstance(variant_node, ast.BoolOp)
                    and type(variant_node.op) is type(node.op)
                    and getattr(variant_node, "lineno", None)
                    == getattr(node, "lineno", None)
                    and getattr(variant_node, "col_offset", None)
                    == getattr(node, "col_offset", None)
                ):
                    variant_node.op = replacement_op_cls()
                    break
            ast.fix_missing_locations(variant)
            results.append(variant)
        return results


# ---------------------------------------------------------------------------
# negate — condition negation
# ---------------------------------------------------------------------------


class ConditionNegator(_LineTargetedTransformer):
    """Wraps the test of If/While nodes in Not() on the target line."""

    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        """Generate variants by negating target-line if/while conditions."""
        results: list[ast.AST] = []
        tree_copy = copy.deepcopy(tree)
        for node in ast.walk(tree_copy):
            if not isinstance(node, (ast.If, ast.While)):
                continue
            if not self._on_target_line(node):
                continue
            # Skip if the test is already negated, to avoid generating `not not x`.
            if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
                continue
            variant = copy.deepcopy(tree_copy)
            for variant_node in ast.walk(variant):
                if (
                    isinstance(variant_node, (ast.If, ast.While))
                    and type(variant_node) is type(node)
                    and getattr(variant_node, "lineno", None)
                    == getattr(node, "lineno", None)
                    and getattr(variant_node, "col_offset", None)
                    == getattr(node, "col_offset", None)
                ):
                    negated = ast.UnaryOp(
                        op=ast.Not(), operand=copy.deepcopy(variant_node.test)
                    )
                    ast.copy_location(negated, variant_node.test)
                    variant_node.test = negated
                    break
            ast.fix_missing_locations(variant)
            results.append(variant)
        return results


# ---------------------------------------------------------------------------
# return — return value mutation
# ---------------------------------------------------------------------------


class ReturnValueMutator(_LineTargetedTransformer):
    """Mutates return statements: True<->False, non-None value -> None."""

    def generate_variants(self, tree: ast.AST) -> list[ast.AST]:
        """Generate variants by mutating target-line return values."""
        results: list[ast.AST] = []
        tree_copy = copy.deepcopy(tree)
        for node in ast.walk(tree_copy):
            if not isinstance(node, ast.Return):
                continue
            if not self._on_target_line(node):
                continue
            return_value = node.value

            # True → False
            if isinstance(return_value, ast.Constant) and return_value.value is True:
                variant = copy.deepcopy(tree_copy)
                for variant_node in ast.walk(variant):
                    if (
                        isinstance(variant_node, ast.Return)
                        and getattr(variant_node, "lineno", None)
                        == getattr(node, "lineno", None)
                        and getattr(variant_node, "col_offset", None)
                        == getattr(node, "col_offset", None)
                        and isinstance(variant_node.value, ast.Constant)
                        and variant_node.value.value is True
                    ):
                        variant_node.value = ast.Constant(value=False)
                        ast.copy_location(variant_node.value, return_value)
                        break
                ast.fix_missing_locations(variant)
                results.append(variant)

            # False → True
            elif isinstance(return_value, ast.Constant) and return_value.value is False:
                variant = copy.deepcopy(tree_copy)
                for variant_node in ast.walk(variant):
                    if (
                        isinstance(variant_node, ast.Return)
                        and getattr(variant_node, "lineno", None)
                        == getattr(node, "lineno", None)
                        and getattr(variant_node, "col_offset", None)
                        == getattr(node, "col_offset", None)
                        and isinstance(variant_node.value, ast.Constant)
                        and variant_node.value.value is False
                    ):
                        variant_node.value = ast.Constant(value=True)
                        ast.copy_location(variant_node.value, return_value)
                        break
                ast.fix_missing_locations(variant)
                results.append(variant)

            # non-None return → return None
            elif return_value is not None and not (
                isinstance(return_value, ast.Constant) and return_value.value is None
            ):
                variant = copy.deepcopy(tree_copy)
                for variant_node in ast.walk(variant):
                    if (
                        isinstance(variant_node, ast.Return)
                        and getattr(variant_node, "lineno", None)
                        == getattr(node, "lineno", None)
                        and getattr(variant_node, "col_offset", None)
                        == getattr(node, "col_offset", None)
                    ):
                        variant_node.value = ast.Constant(value=None)
                        ast.copy_location(variant_node.value, return_value)
                        break
                ast.fix_missing_locations(variant)
                results.append(variant)

        return results


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_OPERATOR_REGISTRY: dict[str, type[_LineTargetedTransformer]] = {
    "arith": ArithmeticOperatorReplacer,
    "comp": ComparisonOperatorReplacer,
    "obo": OffByOneReplacer,
    "bool": BooleanOperatorReplacer,
    "negate": ConditionNegator,
    "return": ReturnValueMutator,
}


def get_operator_class(key: str) -> type[_LineTargetedTransformer]:
    """Return the operator transformer class for the given key.

    Raises:
        ConfigurationError: If the key is not recognized.
    """
    operator_class = _OPERATOR_REGISTRY.get(key)
    if operator_class is None:
        raise ConfigurationError(
            f"Unknown operator key {key!r}. Valid keys: {sorted(_OPERATOR_REGISTRY)}"
        )
    return operator_class
