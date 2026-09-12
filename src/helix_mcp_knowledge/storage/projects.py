"""Project persistence extension point."""

from .database import Database


def project_count(database: Database) -> int:
    with database.connect() as connection:
        return int(connection.execute("SELECT count(*) FROM projects").fetchone()[0])
