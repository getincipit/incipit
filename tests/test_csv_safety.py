import pytest

from incipit.csv_safety import safe_row


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1:A2)", "-1+1", "@SUM(A1:A2)",
                                 "  =1+1", "\t=1+1", "\rtext", "\ntext"])
def test_formula_like_csv_cells_are_exported_as_text(value):
    assert safe_row([value]) == ["'" + value]


def test_safe_csv_cells_remain_unchanged_and_protection_is_idempotent():
    values = ["Ordinary text", "'quoted", "'=1+1", "", None, 0, -1]
    assert safe_row(values) == values
    assert safe_row(safe_row(["=1+1"])) == ["'=1+1"]
