import pty
import os
import sys

def main():
    command = ['/opt/homebrew/bin/gemini']
    print(f"Running '{' '.join(command)}' in a PTY. You should be able to interact with it.")
    try:
        pty.spawn(command)
    except Exception as e:
        print(f"Error spawning process: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
