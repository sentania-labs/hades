import os, sys
def main() -> None: os.execv(os.path.join(os.path.dirname(sys.executable), "crucible"), ["crucible", *sys.argv[1:]])
