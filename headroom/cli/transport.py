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


@transport.command("pool")
@click.option("--admission-fd", required=True, type=click.IntRange(min=3))
@click.option(
    "--max-leases",
    default=8,
    show_default=True,
    type=click.IntRange(min=1, max=8),
    help="Most leases the pool serves at once; admits beyond it are refused.",
)
@click.option(
    "--lease-compression-concurrency",
    default=2,
    show_default=True,
    type=click.IntRange(min=1),
    help="Most compression jobs one lease runs at once on the shared executor.",
)
def pool(admission_fd: int, max_leases: int, lease_compression_concurrency: int) -> None:
    """Serve leases admitted over an inherited socket until the pool drains."""

    from headroom.transport.pool import serve_pool_fd

    try:
        exit_code = serve_pool_fd(
            admission_fd,
            max_leases=max_leases,
            lease_compression_concurrency=lease_compression_concurrency,
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    if exit_code:
        raise click.exceptions.Exit(exit_code)
