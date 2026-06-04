-- ============================================================================
--  Unit Test Advisor  -  Snowflake stored procedure
-- ============================================================================
--  Given a table, returns ONE ROW PER COLUMN identifying which columns are
--  required for a unit test and why. Two modes:
--
--   1) METADATA mode (default) -- CALL advisor('T')
--      Reads DECLARED constraints (DESCRIBE TABLE + SHOW IMPORTED KEYS):
--      NOT NULL / PRIMARY KEY / UNIQUE / FOREIGN KEY / DEFAULT.
--      If a table has no declared constraints, it finds nothing.
--
--   2) PROFILE mode -- CALL advisor('T', TRUE)
--      IGNORES declared constraints and FIGURES OUT candidate keys from the
--      actual DATA: scans the table and infers uniqueness, never-null, and
--      composite keys (column combos that are unique together). Use this on
--      raw tables that have no PK / NOT NULL / etc.
--      NOTE: this scans the table (1 pass for per-column stats, +1 if it has
--      to search composite keys). Inferred keys are CANDIDATES based on current
--      data -- "unique today" is not "guaranteed unique forever" -- so a human
--      should confirm before trusting them.
--
--  Snowflake enforces ONLY 'NOT NULL'. PRIMARY KEY / UNIQUE / FOREIGN KEY are
--  informational (RELY) and NOT enforced; CHECK is unsupported. Those still
--  matter for testing, but as data-quality assertions (no dup keys, no orphans).
--
--  Install once:   run this whole file in a worksheet.
--  Use (metadata): CALL advisor('EMPLOYEES');
--  Use (profile):  CALL advisor('RAW_TABLE', TRUE);
--                  CALL advisor('RAW_TABLE', PROFILE_DATA => TRUE);
--  Only required:  SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE REQUIRED = 'YES';
-- ============================================================================

CREATE OR REPLACE PROCEDURE advisor(TABLE_NAME STRING, PROFILE_DATA BOOLEAN DEFAULT FALSE)
RETURNS TABLE (
    COLUMN_NAME  STRING,
    DATA_TYPE    STRING,
    REQUIRED     STRING,   -- YES = has something to assert; NO = optional/no signal
    PRIORITY     STRING,   -- HIGH | MEDIUM | LOW
    REASON       STRING,   -- metadata: the constraints; profile: the data evidence
    WHAT_TO_TEST STRING    -- concise assertions to cover
)
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
AS
$$
import re
import itertools
from snowflake.snowpark.types import StructType, StructField, StringType

PRIO_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
SORT_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

# Profiling heuristics (data-driven; tune to taste):
MAX_ENUM_DISTINCT = 25     # <= this many distinct values -> looks like an enum
ENUM_MAX_PCT      = 10.0   # ...and <= this % selective -> accepted_values candidate
MIN_KEY_FILL      = 0.5    # a nullable unique key must be populated in >= 50% of rows
MIN_KEY_VALUES    = 2      # ...and have at least this many non-null values


# ── METADATA mode: summarize DECLARED constraints ─────────────────────────────

def _summary(columns, composite_pk):
    pk_keys = ", ".join(composite_pk) if len(composite_pk) > 1 else None
    rows = []
    for col in columns:
        name = col["name"]
        typ  = col["type"] or ""
        reason, test, prios, required = [], [], ["LOW"], False

        if col["primary_key"]:
            required = True
            prios.append("HIGH")
            reason.append("PRIMARY KEY (Snowflake: not enforced)")
            test.append("No duplicate %s" % (pk_keys or name))

        if not col["nullable"]:
            required = True
            prios.append("HIGH")
            reason.append("NOT NULL")
            test.append("Never NULL")

        if col["unique"] and not col["primary_key"]:
            required = True
            prios.append("HIGH")
            reason.append("UNIQUE (Snowflake: not enforced)")
            test.append("No duplicate values")

        if col["foreign_key"]:
            required = True
            prios.append("HIGH")
            reason.append("FOREIGN KEY -> %s (Snowflake: not enforced)" % col["foreign_key"])
            test.append("No orphan rows vs %s" % col["foreign_key"])

        if col["default"] not in (None, ""):
            required = True
            prios.append("MEDIUM")
            reason.append("DEFAULT %s" % col["default"])
            test.append("Default applied when value omitted")

        if not required:
            reason.append("Optional (nullable, no constraint)")
            test.append("NULL-safe handling")

        prio = max(prios, key=lambda p: PRIO_RANK[p])
        rows.append((
            name, typ,
            "YES" if required else "NO",
            prio,
            "; ".join(reason),
            " | ".join(test),
        ))

    rows.sort(key=lambda r: (0 if r[2] == "YES" else 1, SORT_RANK[r[3]]))
    return rows


def _introspect_metadata(session, table_name):
    desc = session.sql("DESCRIBE TABLE " + table_name).collect()
    columns, composite_pk = [], []
    for r in desc:
        d = {k.lower(): v for k, v in r.as_dict().items()}
        name  = d.get("name")
        is_pk = str(d.get("primary key", "N")).upper() == "Y"
        is_uq = str(d.get("unique key", "N")).upper() == "Y"
        nullable = str(d.get("null?", "Y")).upper() == "Y"
        if is_pk:
            composite_pk.append(name)
        columns.append({
            "name": name,
            "type": d.get("type") or "",
            "nullable": nullable,
            "default": d.get("default"),
            "primary_key": is_pk,
            "unique": is_uq,
            "foreign_key": None,
        })

    try:
        fks = session.sql("SHOW IMPORTED KEYS IN TABLE " + table_name).collect()
        fkmap = {}
        for r in fks:
            d = {k.lower(): v for k, v in r.as_dict().items()}
            fkcol = d.get("fk_column_name")
            if fkcol:
                parent = ".".join(p for p in (
                    d.get("pk_database_name"),
                    d.get("pk_schema_name"),
                    d.get("pk_table_name"),
                ) if p)
                fkmap[fkcol] = "%s.%s" % (parent, d.get("pk_column_name"))
        for c in columns:
            if c["name"] in fkmap:
                c["foreign_key"] = fkmap[c["name"]]
    except Exception:
        pass  # no FKs or insufficient privileges

    return _summary(columns, composite_pk)


# ── PROFILE mode: FIGURE OUT keys from the DATA (no declared constraints) ──────

def _qid(name):
    return '"' + str(name).replace('"', '""') + '"'


def _distinct_expr(name, typ):
    # COUNT(DISTINCT ...) errors on semi-structured/geo types; cast those.
    t = (typ or "").upper()
    if t.startswith(("VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY")):
        return "TO_VARCHAR(%s)" % _qid(name)
    return _qid(name)


def _fmt(n):
    return "{:,}".format(int(n))


def _classify(n, nn, nd):
    """From counts -> (required, priority, reason, what_to_test, composite_ok, is_key)."""
    nulls = n - nn
    sel = (100.0 * nd / n) if n else 0.0
    fill = (nn / n) if n else 0.0
    base = "rows=%s; nulls=%s; distinct=%s" % (_fmt(n), _fmt(nulls), _fmt(nd))

    # Unique AND never null -> a primary-key candidate.
    if n > 0 and nd == n:
        return (True, "HIGH",
                base + " (100% unique, 0 nulls) -> CANDIDATE KEY",
                "Unique + not null (treat as primary key)", False, True)

    # Unique among present values, mostly populated -> nullable unique-key candidate.
    if nulls > 0 and nd == nn and nn >= MIN_KEY_VALUES and fill >= MIN_KEY_FILL:
        return (True, "HIGH",
                base + " (unique where present) -> CANDIDATE UNIQUE KEY",
                "No duplicate non-null values; %s nulls present" % _fmt(nulls), False, True)

    # Always populated, not unique.
    if n > 0 and nn == n:
        if nd == 1:
            return (False, "LOW", base + " (constant)",
                    "Always equals the single observed value", False, False)
        if nd <= MAX_ENUM_DISTINCT and sel <= ENUM_MAX_PCT:
            return (True, "MEDIUM", base + " (%s distinct -> low cardinality)" % _fmt(nd),
                    "Never null; values within a known set (accepted_values)", True, False)
        return (True, "MEDIUM", base + " (%.1f%% selective)" % sel,
                "Never null", True, False)

    # Nullable and not unique.
    if nd <= 1:
        return (False, "LOW", base + " (nullable, near-constant)",
                "NULL-safe handling", False, False)
    if nd <= MAX_ENUM_DISTINCT and sel <= ENUM_MAX_PCT:
        return (False, "LOW", base + " (nullable, low cardinality)",
                "Values within a known set when present", False, False)
    return (False, "LOW", base + " (nullable, free-form)",
            "NULL-safe handling", False, False)


def _composite_search(session, table_name, cols, cand, n, size):
    """Return list of column-index tuples of `size` that are unique together."""
    if len(cand) < size:
        return []
    combos = list(itertools.combinations(cand, size))[:20]  # bound query width
    sel = ["COUNT(*) AS N"]
    for k, combo in enumerate(combos):
        expr = ", ".join(_distinct_expr(cols[i]["name"], cols[i]["type"]) for i in combo)
        sel.append("COUNT(DISTINCT %s) AS K_%d" % (expr, k))
    row = session.sql("SELECT " + ", ".join(sel) + " FROM " + table_name).collect()[0]
    st = {k.upper(): v for k, v in row.as_dict().items()}
    return [combo for k, combo in enumerate(combos) if int(st["K_%d" % k]) == n]


def _profile(session, table_name):
    # Column names + types only -- intentionally NOT reading PK/NOT NULL/etc.
    desc = session.sql("DESCRIBE TABLE " + table_name).collect()
    cols = []
    for r in desc:
        d = {k.lower(): v for k, v in r.as_dict().items()}
        cols.append({"name": d.get("name"), "type": d.get("type") or ""})
    if not cols:
        return []

    # One table scan: row count + per-column non-null and distinct counts.
    sel = ["COUNT(*) AS N"]
    for i, c in enumerate(cols):
        sel.append("COUNT(%s) AS NN_%d" % (_qid(c["name"]), i))
        sel.append("COUNT(DISTINCT %s) AS ND_%d" % (_distinct_expr(c["name"], c["type"]), i))
    row = session.sql("SELECT " + ", ".join(sel) + " FROM " + table_name).collect()[0]
    st = {k.upper(): v for k, v in row.as_dict().items()}
    n = int(st["N"])

    out = []              # (sortkey, row6)
    composite_pool = []   # (col_index, distinct_count) eligible to form a key
    single_key = False    # a non-null unique column exists (a clean PK)
    any_key = False       # any single-column key candidate (incl. nullable-unique)

    for i, c in enumerate(cols):
        nn = int(st["NN_%d" % i])
        nd = int(st["ND_%d" % i])
        required, prio, reason, test, comp_ok, is_key = _classify(n, nn, nd)
        if n > 0 and nd == n:
            single_key = True
        if is_key:
            any_key = True
        if comp_ok:
            composite_pool.append((i, nd))
        out.append((
            (0 if required else 1, SORT_RANK[prio], 0, i),
            (c["name"], c["type"], "YES" if required else "NO", prio, reason, test),
        ))

    # Only hunt for composite keys when no single column is already a clean key.
    if not single_key and n > 0:
        found = []
        if len(composite_pool) >= 2:
            composite_pool.sort(key=lambda t: -t[1])       # most selective first
            cand = [i for i, _ in composite_pool[:5]]
            found = _composite_search(session, table_name, cols, cand, n, 2)
            if not found:
                found = _composite_search(session, table_name, cols, cand, n, 3)
        for combo in found[:5]:
            names = ", ".join(cols[i]["name"] for i in combo)
            out.append((
                (0, 0, 1, 10000),
                (names, "", "YES", "HIGH",
                 "combination is unique across %s rows -> CANDIDATE COMPOSITE KEY" % _fmt(n),
                 "Unique together (composite key); each part not null"),
            ))
        if not found and not any_key:
            out.append((
                (1, 2, 1, 20000),
                ("(no key found)", "", "NO", "LOW",
                 "No single or composite candidate key found in current data",
                 "Add a surrogate key, or test business rules instead"),
            ))

    out.sort(key=lambda x: x[0])
    return [r for _, r in out]


# ── Entry point ───────────────────────────────────────────────────────────────

def run(session, table_name, profile_data=False):
    # Guard against SQL injection via the object name (interpolated below).
    if not table_name or not re.match(r'^[A-Za-z0-9_$. "]+$', table_name):
        raise ValueError("Invalid table name: %r" % table_name)

    if profile_data:
        rows = _profile(session, table_name)
    else:
        rows = _introspect_metadata(session, table_name)

    if not rows:
        rows = [("(none)", "", "NO", "LOW", "No columns found for %s" % table_name, "")]

    schema = StructType([
        StructField("COLUMN_NAME",  StringType()),
        StructField("DATA_TYPE",    StringType()),
        StructField("REQUIRED",     StringType()),
        StructField("PRIORITY",     StringType()),
        StructField("REASON",       StringType()),
        StructField("WHAT_TO_TEST", StringType()),
    ])
    return session.create_dataframe(rows, schema)
$$;

-- Examples:
--   CALL advisor('EMPLOYEES');               -- declared constraints
--   CALL advisor('RAW_FOURNISSEUR', TRUE);   -- figure out keys from the data
