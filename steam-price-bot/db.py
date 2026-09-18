"""
The SQLite layer.

One table, a handful of functions that take a connection. No ORM, no models —
the whole persistent state of this bot is "which games is which user watching,
at what target price, and have I already told them about the current sale".
"""

import sqlite3

# One row per (user, game) pair.
#
#   user_id         Discord snowflake of whoever ran /watch. Stored so the
#                   poller knows who to DM; also means two people can watch the
#                   same game at different targets without stepping on each
#                   other.
#   appid           Steam appid.
#   name            Cached display name, fetched once at /watch time so the
#                   polling loop doesn't need a second request per game.
#   target_cents    Target price in cents. Stored as an integer because Steam
#                   reports prices in cents, and comparing integers avoids the
#                   floating-point mess where 19.99 isn't really 19.99.
#   notified_sale   1 if we've already sent the "it's on sale" DM for the sale
#                   that's running right now.
#   notified_target 1 if we've already sent the "it hit your target" DM for the
#                   current dip.
#
# Those two flags are what stop the bot from DMing every 30 minutes for the
# whole length of a sale. They're set when a DM goes out and cleared when the
# condition stops being true, so the next sale starts from a clean slate.
SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    appid           INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    target_cents    INTEGER NOT NULL,
    notified_sale   INTEGER NOT NULL DEFAULT 0,
    notified_target INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, appid)
);
"""


def connect(path):
    """
    Open the database and make sure the table exists.

    Takes a path to the SQLite file (it's created if missing). Returns an open
    connection with row_factory set so queries come back as sqlite3.Row, which
    lets the rest of the code write row["appid"] instead of row[2].
    """
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(SCHEMA)
    connection.commit()
    return connection


def add_watch(connection, user_id, appid, name, target_cents):
    """
    Create a watch, or update the target on one that already exists.

    Takes the connection, the Discord user id, the appid, the game's display
    name and the target price in cents. Returns True if this was a brand new
    watch, False if it replaced an existing one.

    Re-running /watch on a game you already watch is a price change, not an
    error, so the UNIQUE constraint is handled with an upsert. The notified
    flags are reset on update: if you lower your target, you want to be told
    again when the new one is met.
    """
    existing = connection.execute(
        "SELECT id FROM watches WHERE user_id = ? AND appid = ?",
        (user_id, appid),
    ).fetchone()

    connection.execute(
        """
        INSERT INTO watches (user_id, appid, name, target_cents)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id, appid) DO UPDATE SET
            name = excluded.name,
            target_cents = excluded.target_cents,
            notified_sale = 0,
            notified_target = 0
        """,
        (user_id, appid, name, target_cents),
    )
    connection.commit()
    return existing is None


def remove_watch(connection, user_id, appid):
    """
    Delete one of a user's watches.

    Takes the connection, the Discord user id and the appid. Returns True if a
    row was actually deleted, False if they weren't watching that game — which
    is what /unwatch uses to tell the difference between "done" and "you
    weren't watching that".
    """
    cursor = connection.execute(
        "DELETE FROM watches WHERE user_id = ? AND appid = ?",
        (user_id, appid),
    )
    connection.commit()
    return cursor.rowcount > 0


def list_watches(connection, user_id):
    """
    Every watch belonging to one user, for /list.

    Takes the connection and the Discord user id. Returns a list of sqlite3.Row
    ordered by name so the output doesn't shuffle between calls.
    """
    return connection.execute(
        "SELECT * FROM watches WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (user_id,),
    ).fetchall()


def all_watches(connection):
    """
    Every watch in the database, for the polling loop.

    Takes the connection. Returns a list of sqlite3.Row ordered by appid, which
    groups rows for the same game together so the poller can fetch each game's
    price once even when several people watch it.
    """
    return connection.execute("SELECT * FROM watches ORDER BY appid").fetchall()


def set_flags(connection, watch_id, notified_sale, notified_target):
    """
    Write both notification flags back for one watch.

    Takes the connection, the row's id, and the two flags as booleans or ints.
    Returns nothing. Called once per watch per polling cycle, after deciding
    what (if anything) got sent.
    """
    connection.execute(
        "UPDATE watches SET notified_sale = ?, notified_target = ? WHERE id = ?",
        (int(notified_sale), int(notified_target), watch_id),
    )
    connection.commit()
