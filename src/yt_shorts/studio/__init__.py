"""The local studio: a FastAPI app exposing one event's clips and editorial
layer over HTTP, for an operator to correct titles and transcripts before
rendering.

FastAPI is an OPTIONAL dependency of the project as a whole - only modules
under this package (``yt_shorts.studio``) may import it. ``harvest``,
``render`` and ``gallery`` (and every other module outside this package)
must keep working in a virtualenv that never installed it. See
``studio.api`` for the app itself.
"""

from __future__ import annotations
