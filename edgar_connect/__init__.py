from edgar_connect._version import get_versions
from edgar_connect.edgar_connect import EDGARConnect

__version__ = get_versions()["version"]


__all__ = ["EDGARConnect"]
