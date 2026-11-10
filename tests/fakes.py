"""Tiny fake DB used by the Milestone 4 unit tests."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambdas"))


def load_lambda(name):
    path = ROOT / "lambdas" / name / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch("boto3.client"), patch("boto3.resource"):
        spec.loader.exec_module(module)
    return module


def norm(sql):
    return " ".join(sql.split())


class FakeCursor:
    """rules: list of (sql_substring, rows). First matching rule wins."""

    def __init__(self, rules):
        self.rules = rules
        self.executed = []  # (normalised sql, params)
        self._rows = []

    def execute(self, sql, params=None):
        sql = norm(sql)
        self.executed.append((sql, params))
        self._rows = []
        for needle, rows in self.rules:
            if needle in sql:
                self._rows = list(rows)
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sql_containing(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


class FakeConn:
    def __init__(self, rules):
        self.cur = FakeCursor(rules)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def wire(module, rules):
    """Point the module at a FakeConn and return it."""
    conn = FakeConn(rules)
    module.dbconn = lambda c: conn
    module.config = lambda: {}
    return conn
