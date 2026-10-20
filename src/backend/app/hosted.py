"""Shared container entry point; each independent deployment selects its own agent."""

from app.core.settings import get_settings
from app.runtime import run_agent


def main() -> None:
    """Run the configured agent; invalid selection fails typed settings validation."""
    run_agent(get_settings().poc_agent_name)


if __name__ == "__main__":
    main()
