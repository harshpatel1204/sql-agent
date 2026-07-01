"""
db.py — Database connection and schema extraction.

This is Piece 1 of the agent: "Schema awareness."

Before Claude can write a single correct SQL query, it needs to know what
tables exist, what columns they have, and how tables relate to each other
(foreign keys). This module's job is to read that structure directly out
of MySQL's own information_schema — the metadata MySQL keeps about itself
— and format it as plain text that we can hand to Claude as context.

Think of this the same way you'd hand a new hire the ER diagram on day
one: without it, they're guessing table names.
"""
from dotenv import load_dotenv
load_dotenv()
import os
import mysql.connector
from mysql.connector import Error


def get_connection():
    """
    Opens a connection to MySQL using the restricted sql_agent account
    (SELECT-only privileges — see the GRANT statement we ran earlier).

    Credentials are read from environment variables rather than hardcoded
    in the file. This is the same principle as not committing a JWT
    secret to source control: connection details are configuration, not
    code, and they should never end up in version control history.

    On your own machine, you'd set these once in your shell profile or a
    .env file (loaded via python-dotenv), e.g.:
        export SQL_AGENT_DB_HOST=localhost
        export SQL_AGENT_DB_USER=sql_agent
        export SQL_AGENT_DB_PASSWORD=agentpass123
        export SQL_AGENT_DB_NAME=sakila
    """
    try:
        connection = mysql.connector.connect(
            host=os.environ.get("SQL_AGENT_DB_HOST", "localhost"),
            user=os.environ.get("SQL_AGENT_DB_USER", "sql_agent"),
            password=os.environ.get("SQL_AGENT_DB_PASSWORD", "agentpass123"),
            database=os.environ.get("SQL_AGENT_DB_NAME", "sakila"),
        )
        return connection
    except Error as e:
        raise ConnectionError(f"Could not connect to MySQL: {e}")


def get_schema_summary(connection, max_tables=None):
    """
    Reads the database structure and returns it as a single formatted
    string, ready to be inserted into Claude's system prompt.

    For each table we extract:
      - column names and their data types (so Claude knows e.g. that
        `rental_date` is a DATETIME, not a string, which affects how
        it should write date comparisons)
      - which column is the primary key (the column that uniquely
        identifies each row — important so Claude doesn't try to filter
        or join on the wrong column)
      - foreign key relationships (which column in this table points to
        which column in another table — this is what makes JOINs
        possible, and it's the single most common thing a naive NL2SQL
        prompt gets wrong if it isn't given this explicitly)

    `max_tables` lets you cap how many tables get summarized — useful for
    very wide databases where dumping the entire schema would eat up too
    much context. Sakila has ~23 tables, which is small enough we don't
    need this, but it's worth knowing the pattern for when you adapt this
    to a real company's database with 200+ tables.
    """
    cursor = connection.cursor()

    # information_schema is MySQL's built-in metadata database — every
    # MySQL server has one, and it describes the structure of every
    # other database on that server. This is the MySQL-internals
    # knowledge from your PG-DAC coursework paying off directly here.
    cursor.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME
        """,
        (connection.database,),
    )
    table_names = [row[0] for row in cursor.fetchall()]

    if max_tables:
        table_names = table_names[:max_tables]

    schema_lines = []

    for table in table_names:
        schema_lines.append(f"Table: {table}")

        # Columns and types
        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (connection.database, table),
        )
        columns = cursor.fetchall()

        for col_name, data_type, is_nullable, col_key in columns:
            key_marker = ""
            if col_key == "PRI":
                key_marker = " [PRIMARY KEY]"
            elif col_key == "MUL":
                key_marker = " [FOREIGN KEY]"
            schema_lines.append(f"  - {col_name}: {data_type}{key_marker}")

        # Foreign key relationships — this is the join map
        cursor.execute(
            """
            SELECT
                COLUMN_NAME,
                REFERENCED_TABLE_NAME,
                REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
            """,
            (connection.database, table),
        )
        foreign_keys = cursor.fetchall()

        for col_name, ref_table, ref_column in foreign_keys:
            schema_lines.append(
                f"  -> {col_name} references {ref_table}.{ref_column}"
            )

        schema_lines.append("")  # blank line between tables for readability

    cursor.close()
    return "\n".join(schema_lines)


if __name__ == "__main__":
    # Quick standalone check: run `python3 db.py` to confirm the
    # connection works and see what the schema summary actually looks
    # like before we hand it to Claude. This is worth running yourself
    # the first time you set this up on your machine too — always
    # verify Piece 1 in isolation before building Piece 2 on top of it.
    conn = get_connection()
    print(f"Connected to database: {conn.database}")
    print()
    summary = get_schema_summary(conn)
    print(summary)
    conn.close()
