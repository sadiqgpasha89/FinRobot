"""Safe arithmetic over registered facts, with the result's unit inferred (never chosen by the model).

The LLM asks for `(F3 - F5) / F5 * 100`; code computes it and works out that USD - USD = USD, / USD = ratio,
* 100 = percent. A model that labels 3.4 percentage points as "bps" would otherwise corrupt every downstream sentence.
"""
from __future__ import annotations

import ast
import operator as op
import re

from finrobot.tools.factbook import Fact, FactBook

_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Pow: op.pow, ast.Mod: op.mod}
_UN = {ast.USub: op.neg, ast.UAdd: op.pos}
_FUNCS = {"abs": abs, "round": round, "min": min, "max": max}
_ID = re.compile(r"^F\d+$")
RATIO = "ratio"   # dimensionless; a final 'ratio' is reported as a multiple ('x')


class CalcError(ValueError):
    pass


def _add_units(a: str, b: str, sub: bool) -> str:
    if a != b:
        raise CalcError(f"cannot add/subtract {a} and {b}")
    return "pp" if (sub and a == "%") else a   # difference of two percentages is percentage points


def _mul_units(a: str, b: str, other_const: float | None) -> str:
    if a == RATIO and b == RATIO:
        return RATIO
    if RATIO in (a, b):
        u = b if a == RATIO else a
        if u == RATIO:
            return RATIO
        return u
    raise CalcError(f"cannot multiply {a} by {b}")


def _div_units(a: str, b: str) -> str:
    if a == b:
        return RATIO
    if b == RATIO:
        return a
    if a == "USD" and b == "shares":
        return "USD/shares"
    if a == "USD" and b == "USD/shares":
        return "shares"
    raise CalcError(f"cannot divide {a} by {b}. For a percentage change of an amount use (F_new - F_old) / F_old * 100; "
                    "for the change in a margin use compute_metric (operating_margin_change_yoy, ebitda_margin_change_yoy) or F_new - F_old")


def _check_basis(a: str, b: str, op_name: str) -> str:
    """Period consistency. Returns the result's basis, or raises for combinations that silently overstate or mislead."""
    if a == b:
        return a
    if not a or not b:
        return a or b
    if op_name == "div" and a == "instant" and b in ("ttm", "fy"):
        return "mixed"   # leverage-style ratio: balance sheet over a trailing/annual flow
    if op_name == "div" and a == "instant" and b == "q":
        raise CalcError("a balance-sheet figure divided by a single quarter's flow overstates the ratio about 4x. "
                        "Use a trailing-twelve-month figure: compute_metric net_debt_to_ttm_ebitda, or ttm_ebitda / ttm_revenue / ttm_fcf")
    raise CalcError(f"mixed periods ({a} with {b}): compare figures on the same basis (quarter with quarter, trailing-twelve-months with "
                    "trailing-twelve-months)")


def evaluate(expr: str, fb: FactBook) -> tuple[float, str, list[str]]:
    """Returns (value, unit, ids of facts used). Only numbers, F# names, + - * / ** % and abs/round/min/max."""
    used: list[str] = []

    def ev(n: ast.AST) -> tuple[float, str, bool, str]:
        """(value, unit, is_literal, period basis)"""
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value), RATIO, True, ""
        if isinstance(n, ast.Name):
            if not _ID.match(n.id) or n.id not in fb.facts:
                raise CalcError(f"unknown fact id '{n.id}'")
            if n.id not in used:
                used.append(n.id)
            f = fb.facts[n.id]
            return f.value, f.unit, False, f.basis
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            (a, ua, la, ba), (b, ub, lb, bb) = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Div) and b == 0:
                raise CalcError("division by zero")
            if isinstance(n.op, ast.Pow):
                if abs(b) > 10:
                    raise CalcError("exponent too large")
                if ub != RATIO:
                    raise CalcError("exponent must be a plain number")
                return a ** b, ua if (b == 1) else (RATIO if ua == RATIO else ua), False, ba
            if isinstance(n.op, (ast.Add, ast.Sub)):
                if la and not lb:  # a literal adopts the other operand's unit (e.g. F1 + 5)
                    ua = ub
                if lb and not la:
                    ub = ua
                unit = _add_units(ua, ub, isinstance(n.op, ast.Sub))
                basis = _check_basis(ba, bb, "add")
            elif isinstance(n.op, ast.Mult):
                # ratio * 100 -> percent, ratio * 10000 -> bps, percentage points * 100 -> bps
                if (ua == RATIO and not la and lb and b == 100) or (ub == RATIO and not lb and la and a == 100):
                    unit = "%"
                elif (ua == RATIO and not la and lb and b == 10000) or (ub == RATIO and not lb and la and a == 10000):
                    unit = "bps"
                elif (ua == "pp" and lb and b == 100) or (ub == "pp" and la and a == 100):
                    unit = "bps"
                else:
                    unit = _mul_units(ua, ub, None)
                basis = ba or bb
            elif isinstance(n.op, ast.Div):
                unit = _div_units(ua, ub)
                basis = _check_basis(ba, bb, "div")
                if ba == bb and ba:
                    basis = ba   # same-period ratio keeps its period
            else:  # modulo
                unit, basis = ua, ba
            return _BIN[type(n.op)](a, b), unit, False, basis
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            v, u, lit, bs = ev(n.operand)
            return _UN[type(n.op)](v), u, lit, bs
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            args = [ev(a) for a in n.args]
            if not args:
                raise CalcError(f"{n.func.id}() needs arguments")
            units = {u for _, u, lit, _ in args if not lit}
            if len(units) > 1:
                raise CalcError(f"{n.func.id} over mixed units {sorted(units)}")
            bases = {bs for _, _, lit, bs in args if not lit and bs}
            return float(_FUNCS[n.func.id](*[v for v, _, _, _ in args])), (units.pop() if units else RATIO), False, (bases.pop() if len(bases) == 1 else "")
        raise CalcError(f"unsupported expression element: {type(n).__name__}")

    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise CalcError(f"cannot parse '{expr}': {e.msg}") from e
    value, unit, _, _basis_ = ev(tree)
    if not used:
        raise CalcError("an expression must use registered fact ids (F#). Numbers typed in by hand have no source, so they are not allowed")
    return value, ("x" if unit == RATIO else unit), used


def calc_and_register(expr: str, fb: FactBook, label: str) -> Fact:
    value, unit, used = evaluate(expr, fb)
    return fb.add_fact(label or expr, value, unit, f"calc: {expr}", derived=True, inputs=used)
