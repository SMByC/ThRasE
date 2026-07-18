"""
/***************************************************************************
 ThRasE

 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2026 by Xavier Corredor Llano, SMByC
        email                : xavier.corredor.llano@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import ast
import math
import operator
import os
from pathlib import Path

from qgis.core import Qgis
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, "ui", "autofill_dialog.ui"))


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}
_COMPARISON_OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: operator.contains(right, left),
    ast.NotIn: lambda left, right: not operator.contains(right, left),
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}

_MAX_EXPRESSION_LENGTH = 2048
_MAX_AST_NODES = 200
_MAX_AST_DEPTH = 40
_MAX_INTEGER_BITS = 4096


def _check_number(value):
    if type(value) is int and value.bit_length() > _MAX_INTEGER_BITS:
        raise ValueError("integer result is too large")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("non-finite result")
    return value


def _numeric_literal(node):
    return (isinstance(node, ast.Constant) and type(node.value) in (int, float)) or (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) in (int, float)
    )


def _literal_number(node):
    if isinstance(node, ast.Constant):
        return node.value
    return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value


def _validate_tree(tree):
    node_count = 0

    def check_complexity(node, depth=0):
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_AST_NODES or depth > _MAX_AST_DEPTH:
            raise ValueError("expression is too complex")
        for child in ast.iter_child_nodes(node):
            check_complexity(child, depth + 1)

    check_complexity(tree)
    allowed_nodes = (
        ast.Expression, ast.Constant, ast.Name, ast.BinOp, ast.UnaryOp, ast.BoolOp,
        ast.Compare, ast.IfExp, ast.Call, ast.Tuple, ast.List, ast.Set, ast.Load,
        ast.operator, ast.unaryop, ast.boolop, ast.cmpop,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError("unsupported syntax")
        if isinstance(node, ast.Name) and node.id not in {"V", "v", "abs"}:
            raise ValueError("unsupported name")
        if isinstance(node, ast.Constant):
            if not (type(node.value) in (int, float, bool) or node.value is None):
                raise ValueError("unsupported constant")
            if type(node.value) in (int, float):
                _check_number(node.value)
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name) and node.func.id == "abs"
            and len(node.args) == 1 and not node.keywords
        ):
            raise ValueError("unsupported call")
        if isinstance(node, ast.operator) and type(node) not in _BINARY_OPERATORS:
            raise ValueError("unsupported operator")
        if isinstance(node, ast.unaryop) and type(node) not in _UNARY_OPERATORS and not isinstance(node, ast.Invert):
            raise ValueError("unsupported operator")
        if isinstance(node, ast.cmpop) and type(node) not in _COMPARISON_OPERATORS:
            raise ValueError("unsupported comparison")
        if isinstance(node, ast.boolop) and not isinstance(node, (ast.And, ast.Or)):
            raise ValueError("unsupported boolean operator")
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            if not all(_numeric_literal(item) for item in node.elts):
                raise ValueError("only numeric literal containers are supported")
            for item in node.elts:
                _check_number(_literal_number(item))


def _evaluate_expression(expression, pixel_value):
    """Parse and evaluate the deliberately small expression language used by autofill."""
    if not isinstance(expression, str) or len(expression) > _MAX_EXPRESSION_LENGTH:
        raise ValueError("expression is too complex")
    tree = ast.parse(expression, mode="eval")

    _validate_tree(tree)

    def evaluate(node):
        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float) or node.value is None or type(node.value) is bool:
                return node.value
            raise ValueError("unsupported constant")
        if isinstance(node, ast.Name) and node.id in {"V", "v"}:
            return pixel_value
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            if not all(_numeric_literal(item) for item in node.elts):
                raise ValueError("only numeric literal containers are supported")
            values = []
            for item in node.elts:
                if not _numeric_literal(item):
                    raise ValueError("only numeric literal containers are supported")
                values.append(_literal_number(item))
            if isinstance(node, ast.Tuple):
                return tuple(values)
            return set(values) if isinstance(node, ast.Set) else values
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left, right = evaluate(node.left), evaluate(node.right)
            if type(node.op) in (ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift):
                if type(left) is not int or type(right) is not int:
                    raise TypeError("bitwise operations require integers")
                if isinstance(node.op, (ast.LShift, ast.RShift)) and abs(right) > _MAX_INTEGER_BITS:
                    raise ValueError("shift is too large")
            # Bound powers before calculating them, including nested powers.
            if isinstance(node.op, ast.Pow) and (not isinstance(right, (int, float)) or abs(right) > _MAX_INTEGER_BITS):
                raise ValueError("power exponent is too large")
            if isinstance(node.op, ast.Pow) and type(left) is int and type(right) is int:
                if right >= 0 and left.bit_length() * right > _MAX_INTEGER_BITS:
                    raise ValueError("integer result is too large")
            return _check_number(_BINARY_OPERATORS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            operand = evaluate(node.operand)
            if isinstance(node.op, ast.Invert) and type(operand) is not int:
                raise TypeError("bitwise inversion requires an integer")
            return _check_number(_UNARY_OPERATORS[type(node.op)](operand))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
            operand = evaluate(node.operand)
            if type(operand) is not int:
                raise TypeError("bitwise inversion requires an integer")
            return _check_number(~operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "abs":
            if node.keywords or len(node.args) != 1:
                raise ValueError("abs accepts one positional argument")
            return _check_number(abs(evaluate(node.args[0])))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            if isinstance(node.op, ast.And):
                result = evaluate(node.values[0])
                for value in node.values[1:]:
                    if not result:
                        return result
                    result = evaluate(value)
                return result
            result = evaluate(node.values[0])
            for value in node.values[1:]:
                if result:
                    return result
                result = evaluate(value)
            return result
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for comparison, comparator in zip(node.ops, node.comparators, strict=True):
                if type(comparison) not in _COMPARISON_OPERATORS:
                    raise ValueError("unsupported comparison")
                right = evaluate(comparator)
                result = _COMPARISON_OPERATORS[type(comparison)](left, right)
                if not result:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return evaluate(node.body) if evaluate(node.test) else evaluate(node.orelse)
        raise ValueError("unsupported expression")

    return evaluate(tree.body)


def _is_valid_expression(expression):
    try:
        if not isinstance(expression, str) or len(expression) > _MAX_EXPRESSION_LENGTH:
            return False
        tree = ast.parse(expression, mode="eval")
        _validate_tree(tree)
        return True
    except (ArithmeticError, SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return False


class AutoFill(QDialog, FORM_CLASS):
    instance = None

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.setup_gui()
        AutoFill.instance = self

    def setup_gui(self):
        # adjust the column width of the table
        self.AutoFillTable.setColumnWidth(0, 240)
        self.AutoFillTable.setColumnWidth(1, 140)
        # adjust the width of the dialog
        self.resize(440, 480)

        self.QPBtn_ApplyAutoFill.clicked.connect(self.apply_autofill)

    def check_condition(self, condition):
        if condition == "*":
            return True
        if condition is None or condition == "":
            return False
        if _is_valid_expression(condition):
            return True
        self.MsgBar.pushMessage(condition, "Invalid condition", level=Qgis.MessageLevel.Warning, duration=10)
        return False

    def check_value(self, value):
        if value is None or value == "":
            return True
        if _is_valid_expression(value):
            return True
        self.MsgBar.pushMessage(value, "Invalid value", level=Qgis.MessageLevel.Warning, duration=10)
        return False

    def apply_autofill(self):
        from ThRasE.core.editing import LayerToEdit
        from ThRasE.thrase import ThRasE

        # first close active items opened in the table
        self.AutoFillTable.setCurrentItem(None)

        # go through the table to get the condition and value
        autofill_entries = []
        for row in range(self.AutoFillTable.rowCount()):
            condition = self.AutoFillTable.item(row, 0)
            condition = condition.text().strip() if condition else None
            value = self.AutoFillTable.item(row, 1)
            value = value.text().strip() if value else None

            if self.check_condition(condition) and self.check_value(value):
                autofill_entries.append((condition, value))

        if not autofill_entries:
            return

        curr_values = [pixel["value"] for pixel in LayerToEdit.current.pixels]
        new_values = [""] * len(curr_values)

        # apply the autofill
        for condition, value in autofill_entries:
            value_is_empty = value in (None, "")

            for idx_row, curr_value in enumerate(curr_values):
                try:
                    matches = condition == "*" or bool(_evaluate_expression(condition, curr_value))
                    if matches:
                        new_values[idx_row] = None if value_is_empty else _evaluate_expression(value, curr_value)
                except (ArithmeticError, SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
                    self.MsgBar.pushMessage(
                        condition if condition != "*" else value,
                        "Invalid autofill expression",
                        level=Qgis.MessageLevel.Warning,
                        duration=10,
                    )
                    return

        # Finish all conversions before touching the layer, so a runtime error
        # cannot leave a partially-applied autofill behind.
        try:
            rounded_values = [
                round(float(new_value)) if new_value not in [None, ""] else None for new_value in new_values
            ]
        except (ArithmeticError, TypeError, ValueError, OverflowError, MemoryError, RecursionError):
            self.MsgBar.pushMessage(
                "autofill", "Invalid autofill expression", level=Qgis.MessageLevel.Warning, duration=10
            )
            return

        # update the values in the table only after every rule and conversion succeeded
        for idx_row, new_value in enumerate(rounded_values):
            LayerToEdit.current.pixels[idx_row]["new_value"] = new_value

        ThRasE.dialog.set_recode_pixel_table()
        ThRasE.dialog.update_recode_pixel_table()
