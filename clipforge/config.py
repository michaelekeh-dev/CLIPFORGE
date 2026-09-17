"""Settings: config.yaml + .env + a few paths. Import `cfg` and `paths`."""
from __future__ import annotations
import os
import functools
from pathlib import Path
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ASSETS = ROOT / "assets"
DATA = Path(os.environ.get("CLIPFORGE_DATA_DIR") or (ROOT / "data")).resolve()
PROJECTS = DATA / "projects"
CACHE = DATA / "cache"
MODELS = DATA / "models"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "clipforge.db"

for _p in (DATA, PROJECTS, CACHE, MODELS, UPLOADS, CACHE / "downloads"):
    _p.mkdir(parents=True, exist_ok=True)


class Cfg(dict):
    """dict with dotted get: cfg.get('render.fps', 30)."""

    def get(self, key, default=None):  # type: ignore[override]
        cur = self
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _wrap(d):
    if isinstance(d, dict):
        return Cfg({k: _wrap(v) for k, v in d.items()})
    return d


def load_config() -> Cfg:
    with open(ROOT / "config.yaml") as f:
        return _wrap(yaml.safe_load(f) or {})


cfg = load_config()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


@functools.lru_cache(maxsize=1)
def device() -> str:
    """'cuda' when a GPU is usable, otherwise 'cpu'."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def output_size(ratio: str = "9:16") -> tuple[int, int]:
    sizes = cfg.get("render.ratios", {})
    w, h = sizes.get(ratio) or [cfg.get("render.width", 1080), cfg.get("render.height", 1920)]
    return int(w), int(h)
