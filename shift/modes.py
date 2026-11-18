from sqlite3 import Connection


def create_mode_schema(db: Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS elastic_shifts (
            shift_id TEXT PRIMARY KEY NOT NULL REFERENCES calendars(shift_id)
        )
    ''')


def mark_elastic_shift(db: Connection, shift_id: str) -> None:
    db.execute('INSERT INTO elastic_shifts (shift_id) VALUES (?)', (shift_id,))


def is_elastic_shift(db: Connection, shift_id: str) -> bool:
    row = db.execute(
        'SELECT 1 FROM elastic_shifts WHERE shift_id = ?', (shift_id,)
    ).fetchone()
    return row is not None
