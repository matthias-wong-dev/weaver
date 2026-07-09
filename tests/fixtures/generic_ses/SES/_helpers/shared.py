"""Shared helper importable by object files (never discovered as an object)."""


def drop_csv_path(folder):
    from pathlib import Path

    return str(Path(folder) / "drop.csv")
