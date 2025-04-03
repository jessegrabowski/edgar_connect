from edgar_connect._version import get_versions
from edgar_connect.edgar_connect import EDGARConnect
import logging

_log = logging.getLogger(__name__)

if not logging.root.handlers:
    _log.setLevel(logging.INFO)
    if len(_log.handlers) == 0:
        handler = logging.StreamHandler()
        _log.addHandler(handler)

__version__ = get_versions()["version"]

__all__ = ["EDGARConnect"]
