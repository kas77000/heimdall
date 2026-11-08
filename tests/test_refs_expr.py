import pandas as pd
import pytest

from heimdall.core import refs, safeexpr

OUT = {"orders": pd.DataFrame({"sym": ["AAPL", "MSFT", "AAPL"], "qty": [1, 2, 3]}),
       "empty": pd.DataFrame({"sym": []})}


def test_q_list_and_atom_and_rows():
    assert refs.fill_q("sym in {{orders.sym}}", OUT) == "sym in `AAPL`MSFT"
    assert refs.fill_q("qty>{{orders.qty[0]}}", OUT) == "qty>1"
    assert refs.fill_q("{{rows:orders}}", OUT) == "3"


def test_q_table_reference():
    assert refs.fill_q("select from {{table:orders}}", OUT).startswith("select from (flip")


def test_unknown_step_and_column():
    with pytest.raises(refs.ReferenceError_, match="unknown step"):
        refs.fill_q("{{nope.sym}}", OUT)
    with pytest.raises(refs.ReferenceError_, match="no column 'px'"):
        refs.fill_q("{{orders.px}}", OUT)


def test_expand_lines_keeps_keys():
    keyed = refs.expand_lines_keyed("{{orders.sym}} US Equity\nIBM US Equity", OUT)
    assert keyed == [("AAPL US Equity", {"sym": "AAPL"}), ("MSFT US Equity", {"sym": "MSFT"}),
                     ("IBM US Equity", {})]


def test_expand_empty_step_gives_nothing():
    assert refs.expand_lines("{{empty.sym}} US Equity", OUT) == []


def test_text_join_and_table_refused():
    assert refs.fill_text("{{orders.sym}}", OUT) == "AAPL,MSFT"
    with pytest.raises(refs.ReferenceError_):
        refs.fill_text("{{table:orders}}", OUT)


def test_referenced_steps():
    assert refs.referenced_steps("a {{x.c}} {{table:y}} {{rows:z}}") == {"x", "y", "z"}


DF = pd.DataFrame({"px": [10.0, 12.0, None], "lim": [11.0, 11.0, 11.0], "s": ["a", "b", "c"]})


def test_expressions_vectorised():
    assert safeexpr.evaluate("px > lim", DF).tolist() == [False, True, False]
    assert safeexpr.evaluate("abs(px - lim) / lim * 100", DF).round(2).tolist()[:2] == [9.09, 9.09]
    assert safeexpr.evaluate("px > lim or s == 'a'", DF).tolist() == [True, True, False]
    assert safeexpr.evaluate("'hi' if px > lim else 'lo'", DF).tolist() == ["lo", "hi", "lo"]
    assert safeexpr.evaluate("isnull(px)", DF).tolist() == [False, False, True]


@pytest.mark.parametrize("bad", ["px.__class__", "__import__('os')", "open('x')", "px[0]",
                                 "(lambda: 1)()", "[1,2]"])
def test_dangerous_expressions_refused(bad):
    with pytest.raises(safeexpr.ExpressionError):
        safeexpr.parse(bad)


def test_unknown_column_named():
    with pytest.raises(safeexpr.ExpressionError, match="unknown column"):
        safeexpr.evaluate("nope > 1", DF)
