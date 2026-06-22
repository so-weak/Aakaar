"""Download-candidate discovery moved to
``aakaar_caps.caps.file_download.discovery`` (portable). Re-exported here so
existing ``aakaar.capabilities.file_download.discovery`` imports keep working.
"""

from aakaar_caps.caps.file_download.discovery import (  # noqa: F401
    DISCOVERY_JS,
    Candidate,
    Pick,
    decide,
    rank_candidates,
)

__all__ = ["DISCOVERY_JS", "Candidate", "Pick", "decide", "rank_candidates"]
