"""
tools.py — Tool definitions for the Anthropic API.

Earlier I explained that a "tool" in the API sense is just a function
description you hand to Claude: a name, a plain-English explanation of
what it does, and a JSON schema for its arguments. Claude never sees or
runs this code directly — Claude only ever sees this description, decides
"I want to call this," and your own Python code (in agent.py) is what
actually executes it.

For this first version of the agent, we expose exactly one tool:
run_sql_query. Claude's only way of interacting with the database is by
asking us to run a query through this tool — it cannot reach MySQL
directly, which is itself a security boundary worth noticing: the LLM
proposes, your code disposes.
"""

# This is the JSON schema format the Anthropic API expects for tools.
# Compare this to a Swagger/OpenAPI spec, which you already know from
# your examination portal project — same underlying idea: a structured,
# machine-readable description of an operation's name, purpose, and
# parameters, except this one's audience is an LLM instead of API
# documentation tooling.
TOOLS = [
    {
        "name": "run_sql_query",
        "description": (
            "Execute a read-only SQL SELECT query against the Sakila "
            "MySQL database and return the resulting rows. "
            "This tool will REJECT any query that is not a SELECT "
            "statement (no INSERT, UPDATE, DELETE, DROP, ALTER, etc). "
            "It will also reject queries that appear unsafe or "
            "inefficient — for example, a SELECT with no WHERE clause "
            "on a large table. If your query is rejected, you will "
            "receive an explanation of why, and you should write a "
            "corrected query and call this tool again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A single, complete MySQL SELECT statement. "
                        "Must reference only tables and columns that "
                        "exist in the schema provided in the system "
                        "prompt."
                    ),
                }
            },
            "required": ["query"],
        },
    }
]
