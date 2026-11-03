import pytest

from finrobot.tools.calc import CalcError, calc_and_register, evaluate
from finrobot.tools.factbook import FactBook


def book():
    fb = FactBook()
    fb.add_fact("a", 6846e6, "USD", "src")
    fb.add_fact("b", 6430e6, "USD", "src")
    return fb


def test_growth_calculation_and_provenance():
    fb = book()
    f = calc_and_register("(F1 - F2) / F2 * 100", fb, "growth")
    assert f.value == pytest.approx(6.47, abs=0.01) and f.unit == "%" and f.derived and f.inputs == ["F1", "F2"]


@pytest.mark.parametrize("expr", ["__import__('os')", "F1.real", "open('x')", "F9 + 1", "F1 ** 99", "1/0", "[1,2]", "lambda: 1", "F1 if 1 else 2"])
def test_rejects_anything_but_arithmetic(expr):
    with pytest.raises(CalcError):
        evaluate(expr, book())


def test_allowed_functions():
    assert evaluate("max(F1, F2) - min(F1, F2)", book())[0] == 416e6


def pct_book():
    fb = FactBook()
    fb.add_fact("margin now", 18.7, "%", "s")   # F1
    fb.add_fact("margin then", 15.3, "%", "s")  # F2
    return fb


def test_difference_of_percentages_is_percentage_points_not_bps():
    """Regression: a model once labelled 3.4 percentage points as '3 bps'."""
    value, unit, _ = evaluate("F1 - F2", pct_book())
    assert unit == "pp" and value == pytest.approx(3.4)
    value, unit, _ = evaluate("(F1 - F2) * 100", pct_book())
    assert unit == "bps" and value == pytest.approx(340)


def test_unit_inference_for_common_shapes():
    fb = book()
    fb.add_fact("shares", 250e6, "shares", "s")   # F3
    assert evaluate("F1 / F2", fb)[1] == "x"
    assert evaluate("F1 / F2 * 100", fb)[1] == "%"
    assert evaluate("F1 / F3", fb)[1] == "USD/shares"
    assert evaluate("F1 + F2", fb)[1] == "USD"
    assert evaluate("F1 * 2", fb)[1] == "USD"


@pytest.mark.parametrize("expr", ["F1 + F3", "F1 * F2", "F3 / F1", "F1 ** F2"])
def test_dimensionally_wrong_expressions_are_rejected(expr):
    fb = book()
    fb.add_fact("shares", 250e6, "shares", "s")
    with pytest.raises(CalcError):
        evaluate(expr, fb)


def test_typed_in_numbers_are_rejected_as_unsourced():
    """Regression: after a rejected calc the model retyped '(18.7 - 15.3)/15.3*100' by hand, creating a fact with no provenance."""
    with pytest.raises(CalcError, match="registered fact ids"):
        evaluate("(18.7 - 15.3) / 15.3 * 100", book())
    assert evaluate("F1 * 1.5", book())[0] == pytest.approx(6846e6 * 1.5)   # a constant next to a real fact is fine


def basis_book():
    fb = FactBook()
    fb.add_fact("net debt", 22.8e9, "USD", "s", basis="instant")      # F1
    fb.add_fact("quarterly EBITDA", 2.07e9, "USD", "s", basis="q")     # F2
    fb.add_fact("TTM EBITDA", 7.77e9, "USD", "s", basis="ttm")         # F3
    fb.add_fact("other quarter EBITDA", 1.9e9, "USD", "s", basis="q")  # F4
    fb.add_fact("TTM revenue", 26e9, "USD", "s", basis="ttm")          # F5
    return fb


def test_balance_sheet_over_a_single_quarter_is_rejected():
    """Regression: net debt / one quarter's EBITDA gave 11.0x leverage instead of 2.93x in 2 of 6 live runs."""
    with pytest.raises(CalcError, match="overstates"):
        evaluate("F1 / F2", basis_book())
    assert evaluate("F1 / F3", basis_book())[0] == pytest.approx(22.8 / 7.77)      # over TTM is the right ratio


def test_mixed_period_flows_are_rejected_but_same_period_is_fine():
    fb = basis_book()
    with pytest.raises(CalcError, match="mixed periods"):
        evaluate("F2 / F5", fb)           # quarterly EBITDA over TTM revenue
    with pytest.raises(CalcError, match="mixed periods"):
        evaluate("F2 + F3", fb)
    assert evaluate("(F2 - F4) / F4 * 100", fb)[1] == "%"
