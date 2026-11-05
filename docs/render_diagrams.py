"""
Пересобирает PNG схем из Mermaid-блоков docs/ARCHITECTURE.md в docs/diagrams/.

    python docs/render_diagrams.py [--puppeteer-config pp.json] [--scale 2]

Нужен Node.js: схемы рисует `npx @mermaid-js/mermaid-cli`. Порядок имён файлов
соответствует порядку блоков ```mermaid в документе.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NAMES = [
    "01-components",
    "02-message-sequence",
    "03-dispatcher",
    "04-session-lifecycle",
    "05-llm-resilience",
    "06-speech-recognition",
    "07-er-diagram",
    "08-summary-memory",
    "09-deployment",
]

DOCS = Path(__file__).resolve().parent
SOURCE = DOCS / "ARCHITECTURE.md"
OUT_DIR = DOCS / "diagrams"
CONFIG = OUT_DIR / "mermaid-config.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--puppeteer-config", help="JSON-конфиг Puppeteer (например, с --no-sandbox)")
    parser.add_argument("--scale", default="2", help="масштаб PNG (по умолчанию 2)")
    args = parser.parse_args()

    blocks = re.findall(r"```mermaid\n(.*?)\n```", SOURCE.read_text(encoding="utf-8"), re.S)
    if len(blocks) != len(NAMES):
        print(f"В документе {len(blocks)} схем, а имён {len(NAMES)}: обновите NAMES в скрипте.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, block in zip(NAMES, blocks):
            source = Path(tmp) / f"{name}.mmd"
            source.write_text(block, encoding="utf-8")
            command = [
                "npx", "-y", "@mermaid-js/mermaid-cli",
                "-i", str(source), "-o", str(OUT_DIR / f"{name}.png"),
                "-c", str(CONFIG), "-b", "white", "-s", args.scale, "-q",
            ]
            if args.puppeteer_config:
                command += ["-p", args.puppeteer_config]
            print(f"→ {name}.png")
            subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
