"""
validate.py — Query validation gate (Piece 3).

This is the module that separates "I used an LLM to write SQL" from "I
built a system that understands query safety and performance." Every
candidate SQL query Claude generates must pass all three checks in this
file before it ever reaches the database.

The three checks, in the order they run:

  Check 1 — Read-only enforcement
    Rejects any statement that isn't a SELECT. This is application-level
    safety on top of the database-level GRANT we already set up — the
    "defense in depth" concept from earlier. Even if Check 1 somehow
    had a bug, the sql_agent MySQL user literally cannot write data.
    Both layers together mean a single point of failure can't cause harm.

  Check 2 — WHERE clause presence
    Catches queries that would scan an entire table with no filter.
    SELECT * FROM rental with no WHERE clause on 16,044 rows is legal
    SQL that MySQL will happily run to completion — but it's almost
    never what a user actually wants, and on a real production table
    with millions of rows it could bring a server to its knees. We catch
    it early and ask Claude to be more specific.

  Check 3 — EXPLAIN plan analysis
    The most technically interesting check, and the one that uses your
    MySQL optimization background most directly. MySQL's EXPLAIN command
    shows HOW a query will execute — which indexes it'll use, how many
    rows it estimates scanning — without actually running the query.
    We parse that output and flag two specific warning signs:
      - type = 'ALL': a full table scan (no index used at all)
      - key = NULL on a large table: the query planner couldn't find a
        usable index, so it's reading every row

Each check returns a ValidationResult — a small object carrying whether
the check passed, and if not, a plain-English reason the agent can read
and use to rewrite its query. This is important: the reason isn't just
for logging, it gets sent back to Claude as a tool_result so the agent
can self-correct on its next iteration.

Run this file standalone to see all checks tested against real queries:
    python validate.py
"""

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Returned by every check function and by validate_query() itself.

    passed: True if the query cleared this check, False if rejected.
    reason: None when passed=True. A plain-English explanation when
            passed=False — written to be useful to Claude as feedback,
            not just to a human reading logs.
    check_name: which of the three checks produced this result, useful
                for logging and for the verbose output in agent.py.
    """
    passed: bool
    reason: Optional[str]
    check_name: str

    def __str__(self):
        if self.passed:
            return f"[{self.check_name}] PASSED"
        return f"[{self.check_name}] FAILED: {self.reason}"


# ---------------------------------------------------------------------------
# Check 1 — Read-only enforcement
# ---------------------------------------------------------------------------

# The set of SQL keywords that indicate a statement modifies data or schema.
# We use a set (not a list) because membership checks on sets are O(1) —
# a minor point, but good habit when you're checking every query.
_WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "REPLACE", "RENAME", "GRANT", "REVOKE",
}

def check_read_only(query: str) -> ValidationResult:
    """
    Check 1: Confirm the query is a SELECT statement and contains no
    data-modifying keywords.

    We do two things here rather than one:
      a) Confirm the first meaningful keyword is SELECT — this catches
         the most common case of Claude accidentally generating an INSERT
         or UPDATE.
      b) Scan the entire query for any write keyword — this catches more
         subtle cases like a subquery containing DELETE, or a comment
         being used to disguise intent (a known SQL injection pattern
         worth being aware of even in an internal tool).

    Why regex + split rather than a SQL parser? A full SQL parser would
    be more precise, but also a heavyweight dependency for what's
    essentially a keyword check. For our purposes — a controlled
    environment where Claude is generating the SQL — this is sufficient
    and easier to reason about. If you were building this for untrusted
    user input, a proper parser would be the right call.
    """
    # Strip leading/trailing whitespace and normalize to uppercase for
    # comparison. We work on the uppercase version for checks but keep
    # the original for error messages so the feedback Claude receives
    # shows the actual query it wrote, not a mangled uppercase version.
    stripped = query.strip()
    upper = stripped.upper()

    # Check a: must start with SELECT (ignoring leading comments)
    # The regex skips past any /* ... */ or -- style comments at the
    # start before looking for the SELECT keyword.
    first_keyword_match = re.match(
        r'^(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*(\w+)',
        upper,
        re.DOTALL
    )
    if not first_keyword_match:
        return ValidationResult(
            passed=False,
            reason="Could not identify the first SQL keyword. "
                   "Please write a plain SELECT statement.",
            check_name="read_only"
        )

    first_keyword = first_keyword_match.group(1)
    if first_keyword != "SELECT":
        return ValidationResult(
            passed=False,
            reason=f"Query starts with '{first_keyword}' but only SELECT "
                   f"statements are permitted. Please rewrite as a SELECT query.",
            check_name="read_only"
        )

    # Check b: scan entire query for write keywords
    # We tokenize on word boundaries so "INSERTING" doesn't falsely
    # match "INSERT", and "CREATED_AT" doesn't match "CREATE".
    tokens = set(re.findall(r'\b[A-Z]+\b', upper))
    found_write_keywords = tokens & _WRITE_KEYWORDS - {"SELECT"}
    if found_write_keywords:
        return ValidationResult(
            passed=False,
            reason=f"Query contains disallowed keyword(s): "
                   f"{', '.join(sorted(found_write_keywords))}. "
                   f"Only SELECT statements are permitted.",
            check_name="read_only"
        )

    return ValidationResult(passed=True, reason=None, check_name="read_only")


# ---------------------------------------------------------------------------
# Check 2 — WHERE clause presence
# ---------------------------------------------------------------------------

# Tables large enough that an unfiltered scan is worth warning about.
# Sakila is a small dataset so all tables are fast to scan in practice,
# but we apply the check to tables above this row threshold to mirror
# what you'd do on a real production database where some tables have
# millions of rows. The threshold is intentionally low here so you can
# actually see the check fire during testing.
_LARGE_TABLE_THRESHOLD = 1000  # rows

# Tables in Sakila we know are large enough to warrant a WHERE clause.
# In a real system you'd query information_schema for actual row counts
# rather than hardcoding this — but for a demo project, being explicit
# about which tables you're protecting is itself a good talking point.
_LARGE_TABLES = {"rental", "payment", "film_actor", "inventory", "customer"}

def check_where_clause(query: str) -> ValidationResult:
    """
    Check 2: Warn when a query against a known large table has no WHERE
    clause, no LIMIT, and no aggregate function (COUNT, SUM, etc.).

    The logic: if you're querying a large table with no filter and no
    limit, you almost certainly either made a mistake or are about to
    pull back far more data than you need. We let it through if there's
    an aggregate (COUNT(*) FROM rental is fine — you want all the rows
    to count them) or a LIMIT (you know you're taking a sample).

    This check is intentionally lenient — it only fires when ALL THREE
    conditions are true simultaneously:
      - The query touches a known large table
      - There's no WHERE clause
      - There's no aggregate function AND no LIMIT

    A false positive here (blocking a query that was actually fine) is
    more annoying than a false negative (letting through a slightly
    inefficient query), so we err on the side of leniency.
    """
    upper = query.upper()

    # Identify which large tables this query references
    referenced_large_tables = [
        t for t in _LARGE_TABLES if re.search(rf'\b{t.upper()}\b', upper)
    ]

    if not referenced_large_tables:
        # Query doesn't touch any of our flagged large tables — no need
        # to check for a WHERE clause, pass through immediately.
        return ValidationResult(
            passed=True,
            reason=None,
            check_name="where_clause"
        )

    has_where = bool(re.search(r'\bWHERE\b', upper))
    has_limit = bool(re.search(r'\bLIMIT\b', upper))
    has_aggregate = bool(re.search(
        r'\b(COUNT|SUM|AVG|MIN|MAX|GROUP\s+BY)\b', upper
    ))

    if not has_where and not has_limit and not has_aggregate:
        tables_str = ", ".join(referenced_large_tables)
        return ValidationResult(
            passed=False,
            reason=f"Query references large table(s) ({tables_str}) with no "
                   f"WHERE clause, no LIMIT, and no aggregate function. "
                   f"This would scan the entire table. Please add a WHERE "
                   f"clause to filter results, a LIMIT to cap the row count, "
                   f"or an aggregate function (like COUNT or SUM) if you "
                   f"intend to summarise all rows.",
            check_name="where_clause"
        )

    return ValidationResult(passed=True, reason=None, check_name="where_clause")


# ---------------------------------------------------------------------------
# Check 3 — EXPLAIN plan analysis
# ---------------------------------------------------------------------------

def check_explain_plan(query: str, connection) -> ValidationResult:
    """
    Check 3: Run MySQL's EXPLAIN on the query and inspect the execution
    plan for performance warning signs — specifically full table scans
    on large tables.

    EXPLAIN is the key MySQL tool for understanding query performance.
    It shows, for each table the query touches:
      - 'type': how MySQL is accessing the table. 'ALL' means it's
        reading every single row (a full table scan). 'ref', 'range',
        'eq_ref' all mean it's using an index to narrow down rows first.
      - 'key': which index MySQL chose to use. NULL means no index.
      - 'rows': MySQL's estimate of how many rows it'll scan. This is
        an estimate, not an exact count, but it's a useful signal.

    We specifically flag: type = 'ALL' on a table with estimated rows
    above our threshold. A full table scan on a 5-row lookup table is
    fine; a full table scan on a 16,000-row rental table is a problem.

    Why run EXPLAIN rather than just checking the query text? Because
    text analysis can't tell you whether an index actually exists for
    the column you're filtering on. A WHERE clause on customer_id uses
    an index (customer_id is a primary key); a WHERE clause on
    first_name probably doesn't (first_name isn't indexed in Sakila).
    Only EXPLAIN knows which is which, because only EXPLAIN asks MySQL's
    actual query planner.

    Note: EXPLAIN doesn't execute the query — it's safe to run on any
    SELECT statement, including expensive ones. This is exactly why we
    run EXPLAIN *before* execution rather than after.
    """
    cursor = connection.cursor(dictionary=True)
    # dictionary=True means each row comes back as a dict like
    # {'type': 'ALL', 'key': None, 'rows': 16044, ...} rather than a
    # plain tuple — much easier to work with when you're accessing
    # specific named fields from EXPLAIN's output.

    try:
        cursor.execute(f"EXPLAIN {query}")
        explain_rows = cursor.fetchall()
    except Exception as e:
        # EXPLAIN itself failed — usually means the query has a syntax
        # error or references a non-existent table/column. Return this
        # as a validation failure with the raw MySQL error, which is
        # actually useful feedback for Claude to correct its query.
        return ValidationResult(
            passed=False,
            reason=f"Query could not be parsed by MySQL: {str(e)}. "
                   f"Please check table and column names against the schema.",
            check_name="explain_plan"
        )
    finally:
        cursor.close()

    # Inspect each row of the EXPLAIN output (one row per table the
    # query touches — a JOIN across three tables produces three rows).
    warnings = []
    for row in explain_rows:
        table_name = row.get("table", "unknown")
        access_type = row.get("type", "")
        key_used = row.get("key")
        estimated_rows = row.get("rows", 0)

        # Full table scan on a meaningfully large table
        if access_type == "ALL" and estimated_rows > _LARGE_TABLE_THRESHOLD:
            warnings.append(
                f"Table '{table_name}' will be fully scanned "
                f"(~{estimated_rows} rows, no index used). "
                f"Consider adding a WHERE clause on an indexed column "
                f"such as the primary key or a foreign key column."
            )

        # No index used but query planner had options — suggests a
        # filter on a non-indexed column (e.g. WHERE first_name = 'Mary')
        if key_used is None and access_type not in ("ALL", "system", "const"):
            warnings.append(
                f"Table '{table_name}' has no index selected "
                f"(access type: '{access_type}'). "
                f"The query may be slower than expected."
            )

    if warnings:
        return ValidationResult(
            passed=False,
            reason="Query execution plan has performance concerns:\n"
                   + "\n".join(f"  - {w}" for w in warnings),
            check_name="explain_plan"
        )

    return ValidationResult(passed=True, reason=None, check_name="explain_plan")


# ---------------------------------------------------------------------------
# Main entry point — runs all three checks in sequence
# ---------------------------------------------------------------------------

def validate_query(query: str, connection) -> ValidationResult:
    """
    Runs all three checks in order, stopping at the first failure.

    Why stop at the first failure rather than collecting all failures?
    Because the checks build on each other: Check 2 and 3 only make
    sense to run if Check 1 confirmed it's actually a SELECT. And
    Check 3 (EXPLAIN) requires a syntactically valid query — there's
    no point running EXPLAIN on a query that Check 2 already flagged.

    The returned ValidationResult is what gets sent back to Claude as
    a tool_result when validation fails — so the reason field needs to
    be specific enough that Claude can actually correct the query, not
    just know that something was wrong.
    """
    # Check 1: read-only (no database connection needed)
    result = check_read_only(query)
    if not result.passed:
        return result

    # Check 2: WHERE clause (no database connection needed)
    result = check_where_clause(query)
    if not result.passed:
        return result

    # Check 3: EXPLAIN plan (requires a live database connection)
    result = check_explain_plan(query, connection)
    return result


# ---------------------------------------------------------------------------
# Standalone test — run `python validate.py` to see all checks fire
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from db import get_connection

    conn = get_connection()
    print("Connected to database:", conn.database)
    print("=" * 60)

    test_cases = [
        # (description, query, expected: pass or fail)
        (
            "Valid query with WHERE on indexed column",
            "SELECT first_name, last_name, email FROM customer WHERE customer_id = 1",
            "pass"
        ),
        (
            "Check 1 fail: UPDATE statement",
            "UPDATE customer SET active = 0 WHERE customer_id = 1",
            "fail"
        ),
        (
            "Check 1 fail: DROP statement",
            "DROP TABLE rental",
            "fail"
        ),
        (
            "Check 2 fail: large table with no WHERE, LIMIT, or aggregate",
            "SELECT * FROM rental",
            "fail"
        ),
        (
            "Check 2 pass: large table but has COUNT aggregate",
            "SELECT COUNT(*) FROM rental",
            "pass"
        ),
        (
            "Check 2 pass but Check 3 fail: LIMIT doesn't prevent full scan",
            "SELECT * FROM rental LIMIT 10",
            "fail"
            # Check 2 passes (has LIMIT) but Check 3 correctly catches that
            # MySQL still scans the whole rental table to find the first 10
            # rows when there's no WHERE on an indexed column. LIMIT alone
            # doesn't make a query efficient — it just caps the output.
            # This is a real MySQL behavior worth knowing and demonstrating.
        ),
        (
            "Check 3 fail: full table scan on rental (no index on return_date)",
            "SELECT * FROM rental WHERE return_date > '2005-01-01'",
            "fail"
        ),
        (
            "Check 3 pass: uses primary key index on rental",
            "SELECT * FROM rental WHERE rental_id = 1",
            "pass"
        ),
        (
            "Check 3 fail: NOT IN subquery causes full scan (known MySQL behavior)",
            """SELECT c.customer_id, c.first_name, c.last_name
               FROM customer c
               WHERE c.customer_id NOT IN (
                   SELECT DISTINCT customer_id FROM rental
                   WHERE rental_date >= DATE_SUB(NOW(), INTERVAL 90 DAY)
               )""",
            "fail"
            # NOT IN with a subquery prevents MySQL from using the primary key
            # index on customer. The efficient rewrite is LEFT JOIN ... IS NULL.
            # This check firing is correct — it's catching a real inefficiency.
        ),
        (
            "Check 3 pass: JOIN on foreign key — planner uses index",
            """SELECT c.first_name, COUNT(r.rental_id)
               FROM customer c
               JOIN rental r ON c.customer_id = r.customer_id
               WHERE c.last_name = 'SMITH'
               GROUP BY c.customer_id""",
            "pass"
            # MySQL joins on customer_id which is a primary key / foreign key
            # — the planner finds an efficient path even though last_name has
            # no index, because it narrows via the join condition first.
        ),
    ]

    passed_count = 0
    failed_count = 0

    for description, query, expected in test_cases:
        print(f"\nTest: {description}")
        print(f"Expected: {expected.upper()}")
        result = validate_query(query, conn)
        print(result)

        # Check if outcome matched expectation
        outcome_matched = (
            (expected == "pass" and result.passed) or
            (expected == "fail" and not result.passed)
        )
        if outcome_matched:
            print("✓ Outcome matched expectation")
            passed_count += 1
        else:
            print("✗ Outcome did NOT match expectation")
            failed_count += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed_count} matched / {failed_count} unexpected")
    conn.close()
