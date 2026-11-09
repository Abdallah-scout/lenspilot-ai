"""Reset the sandbox to its pristine fixture state.

Run:  python -m sandbox.reset            (uses LENSPILOT_SANDBOX_DIR or var/sandbox)
Run logs in var/runs are NOT deleted: they are evaluation evidence.
"""

from sandbox.manager import Sandbox

if __name__ == "__main__":
    sb = Sandbox()
    sb.reset()
    print(f"Sandbox reset at {sb.root} ({len(sb.catalog())} photos, 0 published).")
