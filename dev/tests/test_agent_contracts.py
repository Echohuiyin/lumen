from pathlib import Path
import sys

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from dev.scripts.check_agent_contracts import load_capabilities, validate_capabilities


def test_agent_capability_manifest_is_consistent():
    errors = validate_capabilities(load_capabilities())
    assert errors == []


if __name__ == "__main__":
    test_agent_capability_manifest_is_consistent()
    print("agent_contracts OK")
