"""Start the WebAtlas web interface:  python app.py   (same as `webatlas ui`)."""

from webatlas.cli import app as cli

if __name__ == "__main__":
    cli(["ui"])
