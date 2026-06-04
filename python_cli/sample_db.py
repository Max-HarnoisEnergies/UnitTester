"""Creates a sample SQLite database with realistic tables for testing."""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "sample.db")


def create_sample_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS departments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            budget      REAL    DEFAULT 0.0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS employees (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name    TEXT    NOT NULL,
            last_name     TEXT    NOT NULL,
            email         TEXT    NOT NULL UNIQUE,
            salary        REAL    NOT NULL CHECK (salary > 0),
            hire_date     TEXT    NOT NULL,
            is_active     INTEGER NOT NULL DEFAULT 1,
            department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
            notes         TEXT
        );

        CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            start_date  TEXT    NOT NULL,
            end_date    TEXT,
            budget      REAL,
            status      TEXT    NOT NULL DEFAULT 'planned'
                            CHECK (status IN ('planned','active','completed','cancelled'))
        );

        CREATE TABLE IF NOT EXISTS employee_projects (
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            role        TEXT    NOT NULL DEFAULT 'contributor',
            PRIMARY KEY (employee_id, project_id)
        );
    """)

    conn.commit()
    conn.close()
    print(f"Sample DB created at: {DB_PATH}")


if __name__ == "__main__":
    create_sample_db()
