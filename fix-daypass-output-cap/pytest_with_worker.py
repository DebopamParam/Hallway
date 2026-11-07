"""pytest with the day pass worker's packages (PydanticAI) appended after the app's, so the app's pins win."""
import sys
sys.path.append("/home/agent/workspace/hallway/daypass/.venv/lib/python3.13/site-packages")
import pytest
sys.exit(pytest.main(sys.argv[1:]))
