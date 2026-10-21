"""Spreadsheet-safe CSV cells; authoritative transcription data stays unchanged."""


def safe_row(values):
    """Force formula-like text to be treated as text by spreadsheet importers.

    CSV quoting only protects delimiters, not formula execution. Prefix only risky text;
    JSON, PDFs and database values must retain the original content.
    """
    return ["'" + value if isinstance(value, str) and value and (
        value.startswith(("\t", "\r", "\n")) or
        value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))) else value
        for value in values]
