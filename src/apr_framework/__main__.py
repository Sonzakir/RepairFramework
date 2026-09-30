"""Package entrypoint for the APR framework CLI."""

from apr_framework.cli.app import main

app = main


if __name__ == "__main__":
    raise SystemExit(main())
