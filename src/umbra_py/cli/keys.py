"""Operator commands for hosted API keys: ``umbra keys list`` and ``revoke``.

There is no HTTP admin route. The file is ``api-keys.json`` beside the catalog
index (the same directory as ``usage-stats.json``). ``list`` prints the email
and the created date. ``revoke`` marks a key revoked. Neither prints the
secret or its hash.
"""

from __future__ import annotations

import click

from ..api_keys import keys_path_for
from ..exceptions import UmbraError
from . import _shared
from ._root import cli


@cli.group()
def keys() -> None:
    """List and revoke hosted API keys stored beside the catalog index.

    Not an HTTP admin route. ``list`` prints the email, the created date, and
    whether the key is active. The secret and its hash are not printed.
    """


@keys.command("list")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Catalog index the key file sits beside (default: $UMBRA_INDEX_DB or "
    "the shared index path). The file is api-keys.json in that directory.",
)
def keys_list(db_path: str | None) -> None:
    """Print email, created date, and status. No hashes."""
    from ..api_keys import ApiKeyStore

    path = keys_path_for(_shared._index_path(db_path))
    if not path.is_file():
        click.echo("No API keys.")
        return
    rows = ApiKeyStore(path).list_public()
    if not rows:
        click.echo("No API keys.")
        return
    click.echo("email\tcreated_at\tstatus")
    for row in rows:
        status = "revoked" if row["revoked"] else "active"
        click.echo(f"{row['email']}\t{row['created_at']}\t{status}")


@keys.command("revoke")
@click.argument("email")
@click.option(
    "--created",
    "created_at",
    default=None,
    help="created_at from `umbra keys list`. Required when the email has more than one active key.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Catalog index the key file sits beside (default: $UMBRA_INDEX_DB or "
    "the shared index path). The file is api-keys.json in that directory.",
)
def keys_revoke(email: str, created_at: str | None, db_path: str | None) -> None:
    """Revoke an active key by email. Does not print the hash."""
    from ..api_keys import ApiKeyStore

    path = keys_path_for(_shared._index_path(db_path))
    if not path.is_file():
        raise click.ClickException(f"No API key file at {path}.")
    try:
        revoked = ApiKeyStore(path).revoke(email, created_at=created_at)
    except UmbraError as exc:
        raise click.ClickException(str(exc)) from exc
    for record in revoked:
        click.echo(f"revoked {record.email} created {record.created_at}")
