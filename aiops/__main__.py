import sys

from aiops.serve import main

if __name__ == "__main__":
    if sys.argv[1:2] != ["serve"]:
        sys.exit("usage: python -m aiops serve --context CTX --prometheus-url URL [options]")
    sys.exit(main(sys.argv[2:]))
