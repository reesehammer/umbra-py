"""The ``umbra`` Click group, the JSON error envelope, and the entry point.

Command modules attach themselves to the :data:`cli` group defined here, so
this module must not import them -- :mod:`umbra_py.cli` does that, in order,
after this one.
"""

from __future__ import annotations

import json
import os
import sys

import click

from .. import __version__
from ..exceptions import UmbraError


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="umbra-py")
def cli() -> None:
    """umbra-py: discover, download and work with Umbra open SAR data."""


def _json_errors_requested() -> bool:
    """Whether a failure should print machine-readable JSON to stderr.

    True when ``UMBRA_JSON`` is set to a truthy value, or when ``--json`` was
    passed on the command line. The first lets an agent force structured errors
    globally; the second means a caller who already asked for JSON output
    (``umbra search --json``) also gets JSON when the command fails, instead of
    a prose line it then has to parse. The check runs in ``main`` after Click's
    context has unwound, so it reads the raw invocation rather than a parsed
    flag.
    """
    env = os.environ.get("UMBRA_JSON", "")
    if env and env.lower() not in ("0", "false", "no"):
        return True
    return "--json" in sys.argv[1:]


def _emit_umbra_error(exc: UmbraError) -> None:
    """Render a domain error to stderr, JSON or prose per :func:`_json_errors_requested`."""
    if _json_errors_requested():
        click.echo(json.dumps(exc.to_dict()), err=True)
    else:
        click.echo(f"error: {exc}", err=True)
        if exc.hint:
            click.echo(f"hint: {exc.hint}", err=True)


def main() -> None:
    """Console entry point with friendly error reporting."""
    try:
        cli.main(standalone_mode=False)
    except click.ClickException as exc:
        # Several subcommands wrap a domain error as ``ClickException(str(exc))
        # from exc`` for Click's usage-error plumbing; recover the underlying
        # UmbraError from ``__cause__`` so its hint and the JSON contract still
        # apply. A genuine usage error (no UmbraError cause) keeps Click's own
        # rendering -- it is not part of the machine-readable error contract.
        cause = exc.__cause__
        if isinstance(cause, UmbraError):
            _emit_umbra_error(cause)
            sys.exit(1)
        exc.show()
        sys.exit(exc.exit_code)
    except UmbraError as exc:
        _emit_umbra_error(exc)
        sys.exit(1)
    except click.exceptions.Abort:
        sys.exit(130)
