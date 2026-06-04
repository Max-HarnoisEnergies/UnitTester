-- ============================================================================
--  Sample schema for the Unit Test Advisor  (Snowflake)
-- ============================================================================
--  Run order:
--    1) This file  (creates sample tables + seed data)
--    2) advisor.sql  (creates the procedure)
--    3) CALL advisor('EMPLOYEES');
--
--  NOTE: Snowflake records PRIMARY KEY / UNIQUE / FOREIGN KEY as informational
--  constraints (it does NOT enforce them) and does NOT support CHECK. The
--  advisor reads these from DESCRIBE TABLE / SHOW IMPORTED KEYS and turns them
--  into data-quality queries. Business rules that would be CHECK constraints in
--  other engines are left as comments below.
-- ============================================================================

CREATE OR REPLACE TABLE departments (
    id          INTEGER AUTOINCREMENT START 1 INCREMENT 1,
    name        STRING        NOT NULL,
    budget      FLOAT         DEFAULT 0.0,
    created_at  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT pk_departments PRIMARY KEY (id),
    CONSTRAINT uq_departments_name UNIQUE (name)
);

CREATE OR REPLACE TABLE employees (
    id            INTEGER AUTOINCREMENT START 1 INCREMENT 1,
    first_name    STRING       NOT NULL,
    last_name     STRING       NOT NULL,
    email         STRING       NOT NULL,
    salary        NUMBER(12,2) NOT NULL,            -- business rule: salary > 0
    hire_date     DATE         NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    department_id INTEGER,
    notes         STRING,
    CONSTRAINT pk_employees PRIMARY KEY (id),
    CONSTRAINT uq_employees_email UNIQUE (email),
    CONSTRAINT fk_employees_dept FOREIGN KEY (department_id) REFERENCES departments(id)
);

CREATE OR REPLACE TABLE projects (
    id          INTEGER AUTOINCREMENT START 1 INCREMENT 1,
    title       STRING       NOT NULL,
    start_date  DATE         NOT NULL,
    end_date    DATE,
    budget      NUMBER(14,2),
    status      STRING       NOT NULL DEFAULT 'planned',  -- rule: planned|active|completed|cancelled
    CONSTRAINT pk_projects PRIMARY KEY (id)
);

CREATE OR REPLACE TABLE employee_projects (
    employee_id INTEGER NOT NULL,
    project_id  INTEGER NOT NULL,
    role        STRING  NOT NULL DEFAULT 'contributor',
    CONSTRAINT pk_emp_proj PRIMARY KEY (employee_id, project_id),
    CONSTRAINT fk_ep_emp  FOREIGN KEY (employee_id) REFERENCES employees(id),
    CONSTRAINT fk_ep_proj FOREIGN KEY (project_id)  REFERENCES projects(id)
);

-- ── Seed data ───────────────────────────────────────────────────────────────
INSERT INTO departments (name, budget) VALUES
    ('Engineering', 500000),
    ('Sales',       250000),
    ('HR',          120000);

INSERT INTO employees (first_name, last_name, email, salary, hire_date, department_id, notes) VALUES
    ('Ada',   'Lovelace', 'ada@corp.com',   120000, '2021-03-01', 1, NULL),
    ('Grace', 'Hopper',   'grace@corp.com', 130000, '2020-06-15', 1, 'Team lead'),
    ('Tim',   'Reed',     'tim@corp.com',    80000, '2022-01-10', 2, NULL);

INSERT INTO projects (title, start_date, end_date, budget, status) VALUES
    ('Migration', '2024-01-01', NULL,         100000, 'active'),
    ('Website',   '2023-05-01', '2023-12-01',  50000, 'completed');

INSERT INTO employee_projects (employee_id, project_id, role) VALUES
    (1, 1, 'contributor'),
    (2, 1, 'lead'),
    (3, 2, 'contributor');

-- ── Try it (after creating the procedure from advisor.sql) ───────────────────
--   CALL advisor('EMPLOYEES');
--   CALL advisor('EMPLOYEE_PROJECTS');
--   CALL advisor('PROJECTS');
--
-- If your session shows only a handle instead of rows, fetch them with:
--   SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
