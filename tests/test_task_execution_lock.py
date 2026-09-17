from sqlalchemy.dialects import postgresql

from app.task_state import task_execution_lock_query


def test_manual_task_execution_uses_the_same_postgres_row_lock_as_worker():
    statement = task_execution_lock_query(42)
    compiled = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "WHERE TASKS.ID =" in compiled
    assert "FOR UPDATE" in compiled
