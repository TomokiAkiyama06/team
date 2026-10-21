"""授業用 Slack AI アシスタント (classbot)."""

__version__ = "0.1.0"


def main() -> None:
    from classbot.cli import main as cli_main

    cli_main()
