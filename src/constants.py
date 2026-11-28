"""Repository-wide constants and default paths."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENDPOINT = "https://acemap.info/api/v1/work/search"
USER_AGENT = "AI3602-PilotProject/1.0"

DEFAULT_INDEX = ROOT / "data/conceptnet_en.sqlite"
DEFAULT_CONCEPTNET_CACHE = ROOT / "data/conceptnet_cache"
DEFAULT_ACEAMP_CACHE = ROOT / "data/acemap_cache"
DEFAULT_OUTPUT = ROOT / "results/search_results.json"
