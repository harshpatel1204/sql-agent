# SQL Data Analysis Agent

A conversational AI agent that answers plain-English questions about a
MySQL database by generating, validating, and executing SQL queries —
then interpreting the results back in plain English.

Built from scratch using the Google Gemini API directly (no agent
framework), to understand what agent frameworks abstract away.

---

## What it does

You type a question like:

```
Your question: How many films are there in each category?
```

The agent:
1. Reads the database schema and gives it to the LLM as context
2. The LLM generates a candidate SQL query
3. The query passes through a three-check validation gate
4. If validation passes, the query runs against MySQL
5. The LLM interprets the results and answers in plain English

```
=== Answer ===
Here is a breakdown of the number of films in each category:
  Action: 64 films, Animation: 66 films, Children: 60 films ...
```

---

## The validation gate (the interesting part)

Most NL2SQL demos skip validation entirely. This agent runs every
candidate query through three checks before it touches the database:

**Check 1 — Read-only enforcement**
Rejects any statement that isn't a SELECT (no INSERT, UPDATE, DELETE,
DROP, etc). This is application-level safety on top of a database-level
GRANT that gives the agent's MySQL user SELECT-only privileges —
two independent layers so a single point of failure can't cause harm.

**Check 2 — WHERE clause presence**
Catches queries against large tables with no WHERE clause, no LIMIT,
and no aggregate function. A `SELECT * FROM rental` with no filter
would scan 16,000+ rows for no reason — this check stops it early
with a specific explanation the LLM can act on.

**Check 3 — EXPLAIN plan analysis**
Runs MySQL's `EXPLAIN` on the candidate query before executing it.
`EXPLAIN` shows how the query will execute — which indexes it'll use,
how many rows it estimates scanning — without actually running it.
The agent flags full table scans on large tables and feeds the warning
back to the LLM, which rewrites and retries.

If any check fails, the rejection reason becomes a `tool_result` the
LLM reads on its next iteration and uses to self-correct. This
retry-on-failure loop is what makes the system genuinely agentic
rather than a fixed pipeline.

---

## Architecture

```
User question
     │
     ▼
1. Schema awareness     db.py reads table/column/FK structure from
                        MySQL's information_schema and feeds it to
                        the LLM as context in the system prompt.
     │
     ▼
2. NL2SQL translation   LLM generates a candidate SQL SELECT query
                        using the schema as a reference.
     │
     ▼
3. Validation gate      validate.py runs three checks:
   ┌─────────────┐      read-only → WHERE clause → EXPLAIN plan
   │ FAIL        │
   │ (retry) ◄───┘      On failure: reason feeds back to LLM,
   │                     which rewrites the query and tries again.
   │ PASS
     │
     ▼
4. Execution            Validated query runs against MySQL via the
                        restricted sql_agent account (SELECT only).
     │
     ▼
5. Interpretation       LLM turns raw result rows into a plain-
                        English answer for the user.
```

---

## Tech stack

- **Python 3.12+**
- **Google Gemini API** (`google-genai`) — LLM and tool calling
- **MySQL 8.x** — database (Sakila sample dataset)
- **mysql-connector-python** — database connection
- **python-dotenv** — credential management

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/sql-agent
cd sql-agent
```

### 2. Install dependencies

```bash
pip install google-genai mysql-connector-python python-dotenv
```

### 3. Set up MySQL

Install MySQL and load the Sakila sample database:

```bash
mysql -u root -p -e "CREATE DATABASE sakila;"
mysql -u root -p sakila < sakila_sql/sakila-schema.sql
mysql -u root -p sakila < sakila_sql/sakila-data.sql
```

Create a restricted user for the agent (SELECT-only):

```sql
CREATE USER 'sql_agent'@'localhost' IDENTIFIED BY 'your-password';
GRANT SELECT ON sakila.* TO 'sql_agent'@'localhost';
FLUSH PRIVILEGES;
```

### 4. Get a Gemini API key

Go to [aistudio.google.com](https://aistudio.google.com), sign in,
and create a free API key under **Get API key**.

### 5. Create a `.env` file

```
SQL_AGENT_DB_HOST=localhost
SQL_AGENT_DB_USER=sql_agent
SQL_AGENT_DB_PASSWORD=your-password
SQL_AGENT_DB_NAME=sakila
GEMINI_API_KEY=your-gemini-key
```

### 6. Verify the database connection

```bash
python db.py
```

Should print the full Sakila schema — 23 tables with columns and
foreign key relationships.

### 7. Run the agent

```bash
python agent.py
```

---

## Example session

```
Sakila SQL Agent
Ask questions about the DVD rental database in plain English.
Type 'exit' or 'quit' to stop.

Your question: What is the most rented film?

--- Agent loop iteration 1 ---
[Candidate SQL]: SELECT f.title, COUNT(r.rental_id) AS rental_count
                 FROM film f JOIN inventory i ON f.film_id = i.film_id
                 JOIN rental r ON i.inventory_id = r.inventory_id
                 GROUP BY f.title ORDER BY rental_count DESC LIMIT 1;
[Validation PASSED - all 3 checks]

--- Agent loop iteration 2 ---
=== Answer ===
The most rented film is 'BUCKET BROTHERHOOD', with a total of 34 rentals.

Your question: exit
Goodbye!
```

---

## Project structure

```
sql-agent/
├── agent.py        Main agent loop and Gemini client
├── db.py           Database connection and schema extraction
├── validate.py     Three-check query validation gate
├── tools.py        Tool definition reference (Anthropic format)
├── .env            Credentials (not committed to git)
├── .gitignore      Excludes .env and __pycache__
└── sakila_sql/
    ├── sakila-schema.sql
    └── sakila-data.sql
```

---

## What I learned building this

- **Agentic loops in practice:** the retry-on-validation-failure loop
  is the concrete implementation of "observe, decide, act, observe
  again" — not an abstraction, but a `while` loop with a conditional
  append to the conversation history.

- **Defense in depth for AI systems:** relying on a single safety
  layer (either database grants OR application validation, not both)
  means one bug breaks the whole guarantee. Both layers together mean
  a failure in one doesn't compromise the system.

- **EXPLAIN before execution:** MySQL's query planner knows things
  about index usage that no amount of text analysis can replicate.
  Running EXPLAIN as a pre-execution check is the difference between
  "we think this query is safe" and "MySQL confirmed it uses an index."

- **LLM self-correction needs specific feedback:** vague rejection
  messages ("query rejected") produce vague rewrites. Specific messages
  ("full table scan on rental, ~16244 rows, consider LEFT JOIN IS NULL
  instead of NOT IN") produce targeted fixes on the next iteration.

---

## Limitations and known behaviors

- The validation threshold (1000 rows) means queries on truly small
  tables pass even without indexes, which is intentional — a full
  scan on a 10-row lookup table is not a performance concern.
- `NOT IN` subqueries on large tables will typically fail Check 3
  on this MySQL version; the agent is prompted to use `LEFT JOIN
  IS NULL` instead, which the query planner handles efficiently.
- The agent sees only the first 50 rows of any result set to keep
  token usage manageable; it summarizes rather than dumps raw data.

---

## Author

Harsh Patel — B.Tech Bioengineering (MIT World Peace University) +
PG-DAC (SunBeam Institute). Java Full Stack and AI/ML developer.

[LinkedIn](https://linkedin.com/in/patel-h-d) |
[GitHub](https://github.com/harshpatel1204)
