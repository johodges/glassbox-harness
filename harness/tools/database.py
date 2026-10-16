"""
query_database: the LLM writes SQL, a small and exact language; the harness decides what SQL may run.

  SCHEMA IN PROMPT   when the router offers the tool, plain Python reads the tables and columns from the database
                     file and writes them into the tool's description: the LLM learns the schema from the prompt
  CHECK SQL          plain Python: exactly ONE statement, and it must be a SELECT
  RUN QUERY          sqlite3 opens the file READ-ONLY (URI mode=ro), stops the query after 2 seconds, and returns
                     at most 50 rows

The limits live in code, not in the prompt. Nothing in the description says "do not delete": ask for a DELETE and
Llama writes one, and CHECK SQL refuses it. Even with CHECK_SQL = False the read-only connection cannot write.
An SQL error (a misspelled column, say) goes back to the LLM as text, with the schema, so it can fix its query.
"""

import re
import sqlite3
import time
from contextlib import closing

from ..config import WORKSPACE_DIR
from .base import Refusal, Tool
from .files import rel

DATABASE = WORKSPACE_DIR / "data" / "fire_tests.sqlite"  # built by workspace/make_examples.py
MAX_ROWS = 50        # the context window is not free: a query never puts more rows than this into the prompt
TIME_LIMIT = 2.0     # seconds before a runaway query (e.g. a recursive one) is stopped
CHECK_SQL = True     # set to False to see the read-only connection refuse a DELETE on its own


def connect(path=DATABASE):
    """Read-only, enforced by SQLite itself: 'file:...?mode=ro' cannot change the file, whatever the SQL says."""
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def schema_text(path=DATABASE):
    """The database file -> one line per table: its columns, types, row count, first-column values and what it holds.
    (The full source of each table comes back with every result: see `sources`.)"""
    with closing(connect(path)) as db:
        about = dict(db.execute("SELECT table_name, about FROM sources"))
        lines = []
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid"):
            columns = ", ".join(f"{c[1]} {c[2]}" for c in db.execute(f"PRAGMA table_info({table})"))
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            first = db.execute(f"SELECT name FROM pragma_table_info('{table}') WHERE cid = 0").fetchone()[0]
            names = [row[0] for row in db.execute(f"SELECT {first} FROM {table} LIMIT 8")]
            values = f" {first} values: {', '.join(repr(n) for n in names)}." if table != "sources" else ""
            lines.append(f"- {table}({columns}): {count} rows.{values} {about.get(table, 'Where each table is from.')}")
    return "\n".join(lines)


def check_sql(sql):
    """-> the one SELECT statement to run. Raises Refusal, with the reason, for anything else."""
    text = re.sub(r"^```\w*|```$", "", sql.strip()).strip().rstrip(";").strip()  # ```sql fences, a final ;
    bare = re.sub(r"'(?:[^']|'')*'", "''", text)                    # string literals may contain ; or keywords
    bare = re.sub(r"--[^\n]*|/\*.*?\*/", " ", bare, flags=re.S)     # and so may comments
    first = (bare.split() or [""])[0].upper()
    if ";" in bare:
        raise Refusal("Refused by the harness: only ONE SQL statement per call is allowed, and it must be a SELECT.")
    if first not in ("SELECT", "WITH") or re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|ATTACH|"
                                                     r"PRAGMA|VACUUM)\b", bare, re.I):
        raise Refusal(f"Refused by the harness: the fire test database is read-only for the agent. Only a SELECT "
                      f"statement may run, and this is {first or 'empty'}, so nothing in the database was changed. Do not "
                      "try again: tell the user that it cannot be changed from here.")
    return text


# =========================================================================================
class QueryDatabaseTool(Tool):
    name = "query_database"
    intro = ("Run one SQL query (SQLite) on the fire-test database and get the rows back. Its numbers come from the "
             "tables in the FP520 design-fires lecture. Use only these tables and columns:\n")
    description = intro + "(the schema is added when the tool is offered)"
    parameters = {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "One SQLite statement, e.g. SELECT tree, "
                                                                "mass_kg FROM tree_fires WHERE moisture_pct > 10"}},
        "required": ["sql"],
    }
    model_id = "sqlite3 (Python standard library, no ML)"
    check_unused = False  # a helper, like read_file: the completion check does not ask about it
    how_it_works = ("No model here: SQLite runs the SQL the LLM wrote. The harness only lets one read-only SELECT "
                    "through, opens the file read-only, and caps the rows that go back into the prompt.")

    def prepare(self, ctx):
        """Called when the router offers the tool: the schema goes into the description, i.e. into the prompt."""
        with ctx.tracer.stage("SCHEMA IN PROMPT", "python", "sqlite3: sqlite_master + PRAGMA table_info (no ML)",
                              "database file", "text in the tool description") as ev:
            schema = schema_text()
            self.description = self.intro + schema
            ctx.tracer.say(schema, kind="python")
            ev.summary = (f"{rel(DATABASE)}: {schema.count(chr(10)) + 1} tables -> {len(schema):,} characters of "
                          "schema in the tool's description")

    def run(self, ctx, sql):
        t = ctx.tracer
        with t.stage("CHECK SQL", "python", "database.check_sql() (no ML)", "SQL text from the LLM",
                     "one SELECT, or a refusal") as ev:
            t.say(sql, kind="python")
            statement, refusal = sql, None
            if CHECK_SQL:
                try:
                    statement = check_sql(sql)
                except Refusal as exc:
                    refusal = exc
            ev.summary = (f"REFUSED: {refusal}" if refusal else "one SELECT statement -> allowed" if CHECK_SQL
                          else "CHECK_SQL is off: passed on unchecked")
        if refusal:  # raised outside the stage, so the trace shows the reason rather than "FAILED"
            raise refusal

        with t.stage("RUN QUERY", "python", f"sqlite3, {rel(DATABASE)} opened read-only (mode=ro)", "SQL",
                     f"at most {MAX_ROWS} rows") as ev:
            t.say(self.how_it_works, kind="python")
            deadline = time.perf_counter() + TIME_LIMIT
            db = connect()
            db.set_progress_handler(lambda: time.perf_counter() > deadline, 10_000)  # True = stop the query
            try:
                cursor = db.execute(statement)
                rows = cursor.fetchmany(MAX_ROWS + 1)
                columns = [d[0] for d in cursor.description or []]
            except sqlite3.Error as exc:  # goes back to the LLM, so it can fix the query
                ev.summary = f"SQLite error: {exc}"
                if "readonly" in str(exc):  # only reachable with CHECK_SQL = False: SQLite itself says no
                    error = Refusal(f"SQLite refused: {exc}. The file is open read-only; nothing was changed. Do not "
                                    "try again: tell the user that the database cannot be changed from here.")
                elif "interrupt" in str(exc):
                    error = ValueError(f"SQLite error: {exc} (the query ran longer than {TIME_LIMIT:g} s)")
                else:
                    error = ValueError(f"SQLite error: {exc}. Fix the SQL. The tables are:\n{schema_text()}")
            else:
                error = None
                ev.summary = (f"{min(len(rows), MAX_ROWS)} row(s) x {len(columns)} column(s)"
                              + (f" (cut to the first {MAX_ROWS})" if len(rows) > MAX_ROWS else ""))
                for row in rows[:10]:
                    t.say(" | ".join(str(v) for v in row), kind="python")
                sources = dict(db.execute("SELECT table_name, source FROM sources"))
            finally:
                db.close()
        if error:
            raise error
        used = {name: source for name, source in sources.items() if re.search(rf"\b{name}\b", statement, re.I)}
        # one {column: value} record per row: a small LLM reads "mass_kg": 18.5 better than a bare list of values
        result = {"sql": statement, "rows": [dict(zip(columns, row)) for row in rows[:MAX_ROWS]], "source": used,
                  "note": "These rows are the answer, from the database. Reply to the user with them now and cite the "
                          "source as written: it is a lecture slide, not a file to read."}
        if len(rows) > MAX_ROWS:
            result["note"] = f"Only the first {MAX_ROWS} rows are shown: add WHERE or LIMIT to narrow the query."
        return {"artifacts": [], "observations": result}
