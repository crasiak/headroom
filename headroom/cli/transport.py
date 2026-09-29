"""CLI for the private, per-launch Headroom transport."""

from __future__ import annotations

import click

from .main import main


@main.group()
def transport() -> None:
    """Serve a prepared, isolated transport binding."""


@transport.command("serve")
@click.option("--control-fd", required=True, type=click.IntRange(min=3))
@click.option("--readiness-fd", required=True, type=click.IntRange(min=3))
@click.option("--receipt-fd", required=True, type=click.IntRange(min=3))
@click.option(
    "--listen-fd",
    envvar="HEADROOM_TRANSPORT_LISTEN_FD",
    type=click.IntRange(min=3),
    help="Serve this inherited loopback listener instead of opening one. Supervisors "
    "set HEADROOM_TRANSPORT_LISTEN_FD, which older releases ignore.",
)
def serve(control_fd: int, readiness_fd: int, receipt_fd: int, listen_fd: int | None) -> None:
    """Read one acquire record and serve until its lease is released."""

    from headroom.transport.runtime import serve_transport_fds

    try:
        exit_code = serve_transport_fds(control_fd, readiness_fd, receipt_fd, listen_fd)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    if exit_code:
        raise click.exceptions.Exit(exit_code)
