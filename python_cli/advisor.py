"""
SQL Unit Test Advisor
=====================
Introspects a SQL table and suggests unit / data-quality tests per column,
tuned to which constraints the target engine actually ENFORCES.

Usage
-----
# Built-in SQLite sample (no args needed):
    python advisor.py --table employees

# Snowflake (convenience flags — password via $SNOWFLAKE_PASSWORD or prompt):
    python advisor.py --table MY_TABLE \
        --account abc12345.us-east-1 --user JDOE \
        --database ANALYTICS --schema PUBLIC --warehouse WH_XS --role ANALYST

# Snowflake with SSO (corporate single sign-on, opens a browser):
    python advisor.py --table ANALYTICS.PUBLIC.MY_TABLE \
        --account abc12345.us-east-1 --user JDOE --authenticator externalbrowser \
        --warehouse WH_XS --role ANALYST

# Any other engine via a raw SQLAlchemy URL:
    python advisor.py --table MyTable \
        --db "postgresql://user:pass@localhost/mydb"
    python advisor.py --table MyTable \
        --db "mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+17+for+SQL+Server"

Install deps:  pip install -r requirements.txt
"""

import argparse
import getpass
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError


# ── Constraint-enforcement profiles ────────────────────────────────────────────
# Different engines enforce different constraints. A "unit test" is only valid if
# the engine actually rejects bad data; otherwise it must become a data-quality
# query that scans for violations. This is the crux of Snowflake support.

@dataclass
class EnforcementProfile:
    name: str            # shown in report; "" means a fully-enforcing engine
    pk: bool
    unique: bool
    fk: bool
    check: bool
    not_null: bool = True


FULLY_ENFORCED = EnforcementProfile(name="", pk=True, unique=True, fk=True, check=True)

# Snowflake enforces ONLY NOT NULL. PK / UNIQUE / FK are metadata for the
# optimizer (RELY) and are NOT enforced; CHECK constraints are not supported.
SNOWFLAKE = EnforcementProfile(
    name="Snowflake", pk=False, unique=False, fk=False, check=False, not_null=True
)


def profile_for(dialect_name: str) -> EnforcementProfile:
    if dialect_name == "snowflake":
        return SNOWFLAKE
    # sqlite / postgresql / mysql / mssql / oracle all enforce the standard set
    return FULLY_ENFORCED


# ── Data model ──────────────────────────────────────────────────────────────────

@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    default: Optional[str]
    primary_key: bool
    unique: bool
    foreign_key: Optional[str]       # "referenced_table.column"
    check_constraint: Optional[str]  # raw check expression if available

@dataclass
class TestSuggestion:
    column: str
    category: str
    priority: str                    # "HIGH" | "MEDIUM" | "LOW"
    tests: list[str] = field(default_factory=list)


PRIO_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

def bump(current: str, candidate: str) -> str:
    return candidate if PRIO_RANK[candidate] > PRIO_RANK[current] else current


# ── Engine construction ─────────────────────────────────────────────────────────

def make_engine(args) -> "object":
    """Build a SQLAlchemy engine from CLI args."""
    # 1) Explicit raw URL wins.
    if args.db:
        return create_engine(args.db)

    # 2) Snowflake convenience path.
    if args.account:
        try:
            from snowflake.sqlalchemy import URL
        except ImportError:
            print("[ERROR] snowflake-sqlalchemy is not installed.\n"
                  "        Run: pip install snowflake-sqlalchemy")
            sys.exit(1)

        params = {
            "account": args.account,
            "user": args.user,
            "database": args.database,
            "schema": args.schema,
            "warehouse": args.warehouse,
            "role": args.role,
        }
        params = {k: v for k, v in params.items() if v}

        if args.authenticator:
            params["authenticator"] = args.authenticator
            # externalbrowser / oauth / SSO flows do not take a password.
            if args.authenticator.lower() not in ("externalbrowser", "oauth"):
                params["password"] = _get_snowflake_password()
        else:
            params["password"] = _get_snowflake_password()

        return create_engine(URL(**params))

    # 3) Default: bundled SQLite sample.
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.db")
    return create_engine(f"sqlite:///{db_path}")


def _get_snowflake_password() -> str:
    pw = os.environ.get("SNOWFLAKE_PASSWORD")
    if pw:
        return pw
    return getpass.getpass("Snowflake password: ")


# ── Schema introspection ────────────────────────────────────────────────────────

def _safe(fn, *a, **kw):
    """Some dialects don't implement every inspector method (e.g. Snowflake
    check constraints). Degrade gracefully instead of crashing."""
    try:
        return fn(*a, **kw)
    except (NotImplementedError, SQLAlchemyError):
        return []


def resolve_table(insp, table_name: str, schema: Optional[str]) -> str:
    """Case-insensitive table lookup (Snowflake upper-cases unquoted names)."""
    names = _safe(insp.get_table_names, schema=schema) or []
    if table_name in names:
        return table_name
    lookup = {n.lower(): n for n in names}
    if table_name.lower() in lookup:
        return lookup[table_name.lower()]

    where = f" in schema '{schema}'" if schema else ""
    print(f"\n[ERROR] Table '{table_name}' not found{where}.")
    print(f"Available tables: {', '.join(names) or '(none)'}")
    sys.exit(1)


def introspect_table(engine, table_name: str, schema: Optional[str]):
    insp = inspect(engine)
    resolved = resolve_table(insp, table_name, schema)

    raw_cols = insp.get_columns(resolved, schema=schema)
    pk_info  = insp.get_pk_constraint(resolved, schema=schema)
    fk_list  = _safe(insp.get_foreign_keys, resolved, schema=schema)
    uq_list  = _safe(insp.get_unique_constraints, resolved, schema=schema)
    ck_list  = _safe(insp.get_check_constraints, resolved, schema=schema)

    pk_cols = set(pk_info.get("constrained_columns", []))

    fk_map: dict[str, str] = {}
    for fk in fk_list:
        for local_col, ref_col in zip(fk["constrained_columns"], fk["referred_columns"]):
            fk_map[local_col] = f"{fk['referred_table']}.{ref_col}"

    uq_cols: set[str] = set()
    for uq in uq_list:
        if len(uq["column_names"]) == 1:
            uq_cols.add(uq["column_names"][0])

    ck_map: dict[str, str] = {}
    for ck in ck_list:
        expr = ck.get("sqltext", "") or ""
        for col in raw_cols:
            if col["name"].lower() in expr.lower():
                ck_map[col["name"]] = expr

    columns: list[ColumnInfo] = []
    for col in raw_cols:
        name = col["name"]
        columns.append(ColumnInfo(
            name=name,
            type=str(col["type"]),
            nullable=col["nullable"],
            default=str(col["default"]) if col["default"] is not None else None,
            primary_key=name in pk_cols,
            unique=name in uq_cols,
            foreign_key=fk_map.get(name),
            check_constraint=ck_map.get(name),
        ))

    return resolved, columns, pk_info.get("constrained_columns", [])


# ── Analysis logic ───────────────────────────────────────────────────────────────

def analyze(table_fqn: str, columns: list[ColumnInfo],
            composite_pk: list[str], prof: EnforcementProfile) -> list[TestSuggestion]:
    tag = f"[{prof.name}] " if prof.name else ""
    suggestions: list[TestSuggestion] = []

    for col in columns:
        tests: list[str] = []
        cats: list[str] = []
        priority = "LOW"

        # NOT NULL — enforced everywhere we support (incl. on PK columns).
        if not col.nullable:
            if not col.primary_key:
                cats.append("Required Field")
            priority = bump(priority, "HIGH")
            tests.append(f"Insert NULL into '{col.name}' -> expect NOT NULL violation")
            if _is_text(col.type):
                tests.append(f"Insert empty string for '{col.name}' -> verify business rule")

        # PRIMARY KEY
        if col.primary_key:
            cats.insert(0, "Primary Key")
            priority = bump(priority, "HIGH")
            if prof.pk:
                if len(composite_pk) > 1:
                    tests.append("Insert duplicate composite PK -> expect constraint error")
                else:
                    tests.append(f"Insert duplicate '{col.name}' -> expect PK/unique error")
            else:
                if len(composite_pk) > 1:
                    cols = ", ".join(composite_pk)
                    tests.append(f"{tag}Composite PK NOT enforced -- data-quality test: "
                                 f"SELECT {cols}, COUNT(*) FROM {table_fqn} "
                                 f"GROUP BY {cols} HAVING COUNT(*) > 1  (must be 0 rows)")
                else:
                    tests.append(f"{tag}PK NOT enforced -- data-quality test: "
                                 f"SELECT {col.name}, COUNT(*) FROM {table_fqn} "
                                 f"GROUP BY {col.name} HAVING COUNT(*) > 1  (must be 0 rows)")
                tests.append(f"{tag}Verify INSERT/MERGE logic dedupes on the key")
            tests.append("Verify IDENTITY/sequence/auto-increment yields unique values")

        # FOREIGN KEY
        if col.foreign_key:
            cats.append("Foreign Key")
            priority = bump(priority, "HIGH")
            ref_table = col.foreign_key.split(".")[0]
            ref_col = col.foreign_key.split(".")[1] if "." in col.foreign_key else "id"
            if prof.fk:
                tests.append(f"Insert valid '{col.name}' -> matches {col.foreign_key} -> success")
                tests.append(f"Insert invalid '{col.name}' (no match) -> expect FK error")
                tests.append(f"Delete parent row in {ref_table} -> verify ON DELETE behavior")
            else:
                tests.append(f"{tag}FK NOT enforced -- orphan check: "
                             f"SELECT c.{col.name} FROM {table_fqn} c "
                             f"LEFT JOIN {ref_table} p ON c.{col.name} = p.{ref_col} "
                             f"WHERE c.{col.name} IS NOT NULL AND p.{ref_col} IS NULL  (must be 0 rows)")
                tests.append(f"{tag}Validate referential integrity in ETL/app before load")
            if col.nullable:
                tests.append(f"Insert NULL '{col.name}' -> allowed (nullable FK)")

        # UNIQUE (single-column, non-PK)
        if col.unique and not col.primary_key:
            cats.append("Unique")
            priority = bump(priority, "HIGH")
            if prof.unique:
                tests.append(f"Insert two rows with same '{col.name}' -> expect unique violation")
                tests.append(f"Insert NULL '{col.name}' twice -> verify NULL uniqueness rule")
            else:
                tests.append(f"{tag}UNIQUE NOT enforced -- data-quality test: "
                             f"SELECT {col.name}, COUNT(*) FROM {table_fqn} "
                             f"GROUP BY {col.name} HAVING COUNT(*) > 1  (must be 0 rows)")

        # DEFAULT (supported & applied by all engines we target)
        if col.default is not None and col.default not in ("None", ""):
            cats.append("Has Default")
            priority = bump(priority, "MEDIUM")
            tests.append(f"Insert row omitting '{col.name}' -> verify default {col.default} applied")
            tests.append(f"Insert explicit value -> verify default is overridden")

        # CHECK constraint
        if col.check_constraint:
            cats.append("Check Constraint")
            priority = bump(priority, "HIGH")
            if prof.check:
                tests.append(f"Insert value violating CHECK ({col.check_constraint}) -> expect error")
                tests.append("Insert boundary value satisfying CHECK -> expect success")
            else:
                tests.append(f"{tag}CHECK not enforced -- enforce '{col.check_constraint}' "
                             f"in app/ETL; add data-quality test asserting all rows satisfy it")

        # Purely optional column (nothing above fired)
        if col.nullable and not col.foreign_key and not col.primary_key and not cats:
            cats.append("Optional Field")
            tests.append(f"Insert NULL for '{col.name}' -> should succeed")
            tests.append(f"Verify SELECT handles NULL '{col.name}' in aggregations")

        # Type-driven hints
        t = col.type.lower()
        if _is_text(col.type) and not col.nullable:
            tests.append(f"Insert max-length string for '{col.name}' -> verify length handling")
        if any(x in t for x in ("int", "integer", "bigint", "smallint", "number", "numeric")):
            tests.append(f"Insert 0 and negative values for '{col.name}' -> verify business rules")
        if any(x in t for x in ("real", "float", "double", "decimal", "money")):
            tests.append(f"Insert 0.0 and very large/small values for '{col.name}'")
        if any(x in t for x in ("date", "time", "timestamp")):
            tests.append(f"Insert past, present, and future values for '{col.name}'")
            tests.append(f"Insert invalid/out-of-range value for '{col.name}' -> expect error")

        if tests:
            suggestions.append(TestSuggestion(
                column=col.name,
                category=" | ".join(cats) if cats else "General",
                priority=priority,
                tests=tests,
            ))

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    suggestions.sort(key=lambda s: order[s.priority])
    return suggestions


def _is_text(type_str: str) -> bool:
    return any(t in type_str.lower()
               for t in ("char", "text", "varchar", "nvarchar", "string", "clob"))


# ── Output ───────────────────────────────────────────────────────────────────────

PRIORITY_COLORS = {"HIGH": "\033[91m", "MEDIUM": "\033[93m", "LOW": "\033[92m"}
RESET = "\033[0m"

def print_report(table_fqn, columns, suggestions, composite_pk, prof, use_color):
    def out(s: str = ""):
        enc = sys.stdout.encoding or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc))

    def color(p):
        return PRIORITY_COLORS.get(p, "") if use_color else ""
    rst = RESET if use_color else ""

    out(f"\n{'='*74}")
    out(f"  Unit Test Advisor  --  Table: {table_fqn}")
    out(f"{'='*74}")

    if prof.name == "Snowflake":
        out("\n  [!] Dialect: SNOWFLAKE")
        out("      Snowflake enforces ONLY 'NOT NULL'. PRIMARY KEY / UNIQUE / FOREIGN KEY")
        out("      are informational (not enforced); CHECK is unsupported. Tests for those")
        out("      are emitted as data-quality queries, not 'expect-error' assertions.")

    out(f"\n  Columns ({len(columns)} total):")
    for col in columns:
        flags = []
        if col.primary_key:      flags.append("PK")
        if not col.nullable:     flags.append("NOT NULL")
        if col.unique:           flags.append("UNIQUE")
        if col.foreign_key:      flags.append(f"FK->{col.foreign_key}")
        if col.check_constraint: flags.append("CHECK")
        if col.default:          flags.append(f"DEFAULT={col.default}")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        out(f"    * {col.name:25s} {col.type:18s}{flag_str}")

    if len(composite_pk) > 1:
        out(f"\n  Composite PK: ({', '.join(composite_pk)})")

    out(f"\n{'-'*74}")
    out("  Suggested Tests")
    out(f"{'-'*74}")

    for s in suggestions:
        out(f"\n  [{color(s.priority)}{s.priority}{rst}] {s.column}  ({s.category})")
        for t in s.tests:
            out(f"       - {t}")

    high   = sum(1 for s in suggestions if s.priority == "HIGH")
    medium = sum(1 for s in suggestions if s.priority == "MEDIUM")
    low    = sum(1 for s in suggestions if s.priority == "LOW")
    total  = sum(len(s.tests) for s in suggestions)

    out(f"\n{'='*74}")
    out(f"  Summary: {total} test cases across {len(suggestions)} columns "
        f"(HIGH={high}  MEDIUM={medium}  LOW={low})")
    out(f"{'='*74}\n")


# ── Entry point ──────────────────────────────────────────────────────────────────

def parse_table_arg(table: str, schema: Optional[str]):
    """Accept TABLE, SCHEMA.TABLE, or DATABASE.SCHEMA.TABLE."""
    parts = table.split(".")
    name = parts[-1]
    if schema is None and len(parts) >= 2:
        schema = parts[-2]
    return name, schema


def main():
    p = argparse.ArgumentParser(
        description="Analyze a SQL table and suggest unit / data-quality tests.")
    p.add_argument("--table", required=True,
                   help="Table name (TABLE, SCHEMA.TABLE, or DB.SCHEMA.TABLE)")
    p.add_argument("--db", help="Raw SQLAlchemy URL (overrides Snowflake flags)")
    p.add_argument("--schema", help="Schema name (defaults parsed from --table)")

    sf = p.add_argument_group("Snowflake connection")
    sf.add_argument("--account", help="Snowflake account identifier")
    sf.add_argument("--user", help="Snowflake username")
    sf.add_argument("--database", help="Snowflake database")
    sf.add_argument("--warehouse", help="Snowflake warehouse")
    sf.add_argument("--role", help="Snowflake role")
    sf.add_argument("--authenticator",
                    help="e.g. 'externalbrowser' for SSO, 'oauth', or leave blank for password")

    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    args = p.parse_args()

    if sys.platform == "win32":
        os.system("")  # enable ANSI escape handling in modern Windows terminals
    use_color = (not args.no_color) and sys.stdout.isatty()

    table_name, schema = parse_table_arg(args.table, args.schema)

    engine = make_engine(args)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (OperationalError, SQLAlchemyError) as e:
        print(f"[ERROR] Cannot connect to database:\n  {e}")
        sys.exit(1)

    prof = profile_for(engine.dialect.name)
    resolved, columns, composite_pk = introspect_table(engine, table_name, schema)
    table_fqn = f"{schema}.{resolved}" if schema else resolved
    suggestions = analyze(table_fqn, columns, composite_pk, prof)
    print_report(table_fqn, columns, suggestions, composite_pk, prof, use_color)


if __name__ == "__main__":
    main()
