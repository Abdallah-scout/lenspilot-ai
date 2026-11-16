"""Reset the sandbox to its pristine fixture state.

Run between demos: python -m sandbox.reset
Uses LENSPILOT_SANDBOX_DIR or the sandbox under LENSPILOT_DATA_DIR (default var).
Run logs are kept as evaluation evidence.
"""

from agent.config import load_env_file
from sandbox.manager import Sandbox

if __name__ == "__main__":
    load_env_file()
    sb = Sandbox()
    sb.reset()
    print(f"Sandbox reset at {sb.root} ({len(sb.catalog())} photos, 0 published).")
