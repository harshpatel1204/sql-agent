"""
agent.py — Week 2 version with Google Gemini client.

Identical logic to the Anthropic version — same five pieces, same
agentic loop, same validation gate. The only changes are the three
spots where the Anthropic client appeared, swapped for google-genai.

What changed:
  1. Import: `anthropic` -> `google.genai` + `google.genai.types`
  2. Client init: `anthropic.Anthropic()` -> `genai.Client()`
  3. Tool definition format: Anthropic JSON schema dict ->
     `types.Tool(function_declarations=[types.FunctionDeclaration(...)])`
  4. API call: `client.messages.create(...)` ->
     `client.models.generate_content(...)`
  5. Response parsing: `.content` blocks with `.type` ->
     `.candidates[0].content.parts` with `.function_call` attribute

Everything else — db.py, tools.py, validate.py, the agentic loop
structure, the validation gate, result formatting — is unchanged.
This is exactly what "good architecture makes adding a new piece feel
like slotting in a module" looks like in practice.
"""
from dotenv import load_dotenv
load_dotenv()
import os
import sys

from google import genai
from google.genai import types

from db import get_connection, get_schema_summary
from validate import validate_query


# ---------------------------------------------------------------------------
# Tool definition — Gemini format
# ---------------------------------------------------------------------------
# In the Anthropic version, tool definitions lived in tools.py as plain
# JSON dicts. Gemini uses typed objects instead (FunctionDeclaration,
# Schema), but the information is identical: name, description, and the
# shape of the arguments Claude/Gemini is allowed to pass.
#
# We define this here rather than in tools.py because it's
# Gemini-specific — tools.py stays as the Anthropic format reference.

GEMINI_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="run_sql_query",
            description=(
                "Execute a read-only SQL SELECT query against the Sakila "
                "MySQL database and return the resulting rows. "
                "This tool will REJECT any query that is not a SELECT "
                "statement (no INSERT, UPDATE, DELETE, DROP, ALTER, etc). "
                "It will also reject queries that appear unsafe or "
                "inefficient — for example, a SELECT with no WHERE clause "
                "on a large table, or a query whose EXPLAIN plan shows a "
                "full table scan. If your query is rejected, you will "
                "receive an explanation of why, and you should write a "
                "corrected query and call this tool again."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(
                        type="STRING",
                        description=(
                            "A single, complete MySQL SELECT statement. "
                            "Must reference only tables and columns that "
                            "exist in the schema provided in the system "
                            "prompt. Common fix for full table scan: use "
                            "LEFT JOIN ... IS NULL instead of NOT IN."
                        )
                    )
                },
                required=["query"]
            )
        )
    ]
)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt(schema_summary: str) -> str:
    return f"""You are a SQL assistant for a DVD rental business database
called Sakila. You help non-technical users answer questions about their
data by writing and running SQL queries.

You have access to a tool called run_sql_query. Use it to run SELECT
queries against the database to answer the user's question.

Rules you must follow:
- Only write SELECT statements. Never attempt INSERT, UPDATE, DELETE,
  DROP, ALTER, or any other statement that modifies data or schema.
- Only reference tables and columns that actually appear in the schema
  below. Do not guess or invent column names.
- After you receive query results, explain the answer in plain English.
  Do not just dump the raw rows back at the user — interpret them.
- If a query you ran returns zero rows, say so plainly rather than
  inventing an answer.
- If a query is rejected with a validation error, read the reason
  carefully and rewrite the query to address the specific issue raised.
  Common fixes:
    * Full table scan -> add a WHERE clause on an indexed column
      (primary keys and foreign keys are indexed in this database)
    * NOT IN subquery causing full scan -> rewrite as LEFT JOIN with
      IS NULL check instead, like:
      SELECT c.customer_id FROM customer c
      LEFT JOIN rental r ON c.customer_id = r.customer_id
        AND r.rental_date >= DATE_SUB(NOW(), INTERVAL 90 DAY)
      WHERE r.rental_id IS NULL
    * Missing WHERE clause -> add a specific filter

Here is the database schema:

{schema_summary}
"""


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def run_agent(user_question: str, verbose: bool = True) -> str:
    """
    Runs one full turn of the agent using the Gemini API.

    The loop structure is identical to the Anthropic version:
      - Send question + schema + tool definition to the model
      - If model requests a tool call, validate and execute it
      - Send result back, repeat until model gives a plain text answer
      - Return that final answer

    The main structural difference from Anthropic's API: Gemini returns
    parts inside candidates[0].content.parts, and function calls are
    identified by checking whether part.function_call is not None,
    rather than checking part.type == "tool_use".
    """
    # Reads GEMINI_API_KEY from environment — same pattern as the
    # Anthropic client reading ANTHROPIC_API_KEY
    client = genai.Client()

    db_connection = get_connection()
    schema_summary = get_schema_summary(db_connection)
    system_prompt = build_system_prompt(schema_summary)

    # Gemini uses a `contents` list rather than `messages`, but the
    # concept is identical: the full conversation history sent on every
    # request, because the API has no memory between calls.
    # Each entry is a Content object with a role and a list of Parts.
    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=user_question)]
        )
    ]

    max_iterations = 8  # allows for validation retries

    for iteration in range(max_iterations):
        if verbose:
            print(f"\n--- Agent loop iteration {iteration + 1} ---")

        response = client.models.generate_content(
            model="models/gemini-2.5-flash",   # fast, free-tier friendly model
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[GEMINI_TOOL],
                max_output_tokens=2048,
            )
        )

        # Extract parts from the first (and usually only) candidate
        parts = response.candidates[0].content.parts

        # Separate text parts from function call parts
        # In Gemini's API: part.function_call is not None when the model
        # wants to call a tool; part.text is set when it's plain text.
        function_call_parts = [p for p in parts if p.function_call is not None]
        text_parts = [p for p in parts if hasattr(p, 'text') and p.text]

        if verbose:
            for p in text_parts:
                print(f"[Gemini says]: {p.text}")

        # No function calls = model is done, this is the final answer
        if not function_call_parts:
            return text_parts[0].text if text_parts else "(no response)"

        # Model wants to call a tool — append its response to history
        # before we can send tool results back
        contents.append(
            types.Content(
                role="model",
                parts=parts  # include ALL parts, text + function calls
            )
        )

        # Execute each requested function call, collect results
        result_parts = []
        for part in function_call_parts:
            fn = part.function_call
            if fn.name == "run_sql_query":
                query = fn.args["query"]
                if verbose:
                    print(f"[Candidate SQL]: {query}")

                result_text = execute_query_with_validation(
                    db_connection, query, verbose
                )

                # Gemini tool results use Part.from_function_response()
                # — equivalent to Anthropic's tool_result content block
                result_parts.append(
                    types.Part.from_function_response(
                        name="run_sql_query",
                        response={"result": result_text}
                    )
                )

        # Send results back as a user-role Content with function
        # response parts — closes the loop for this iteration
        contents.append(
            types.Content(
                role="user",
                parts=result_parts
            )
        )

    return "Agent did not produce a final answer within the iteration limit."


# ---------------------------------------------------------------------------
# Query execution with validation (unchanged from Anthropic version)
# ---------------------------------------------------------------------------

def execute_query_with_validation(
    connection, query: str, verbose: bool = True
) -> str:
    """
    Runs the three validation checks, then executes if all pass.
    Identical logic to the Anthropic version — validation is
    completely independent of which LLM client we're using.
    """
    validation_result = validate_query(query, connection)

    if not validation_result.passed:
        if verbose:
            print(f"[Validation FAILED - {validation_result.check_name}]:")
            print(f"  {validation_result.reason}")
        return (
            f"VALIDATION FAILED ({validation_result.check_name}): "
            f"{validation_result.reason}"
        )

    if verbose:
        print("[Validation PASSED - all 3 checks]")

    cursor = connection.cursor()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        column_names = (
            [desc[0] for desc in cursor.description]
            if cursor.description else []
        )

        if not rows:
            return "Query executed successfully but returned 0 rows."

        display_rows = rows[:50]
        result_lines = [", ".join(column_names)]
        for row in display_rows:
            result_lines.append(", ".join(str(v) for v in row))

        result_text = "\n".join(result_lines)
        if len(rows) > 50:
            result_text += f"\n... ({len(rows)} total rows, showing first 50)"

        return result_text

    except Exception as e:
        return f"Query failed with error: {str(e)}"
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Sakila SQL Agent")
    print("Ask questions about the DVD rental database in plain English.")
    print("Type 'exit' or 'quit' to stop.")
    print("=" * 60)

    while True:
        print()
        question = input("Your question: ").strip()

        if not question:
            continue

        if question.lower() in ("exit", "quit"):
            print("Goodbye!")
            break

        answer = run_agent(question)
        print(f"\n=== Answer ===\n{answer}")