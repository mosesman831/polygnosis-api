"""PolyGnosis API — adversarial multi-model consensus over HTTP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("polygnosis-api")
except PackageNotFoundError:  # not installed (e.g. running from source tree)
    __version__ = "0.2.0"
