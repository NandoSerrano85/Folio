"""folio_core — the shared spine for the Folio application.

Importable by both the worker and the portal services. Provides configuration,
database wiring, ORM models, hashing, EXIF stamping, storage-path construction,
token encryption, and logging.
"""

__version__ = "0.1.0"

__all__ = [
    "__version__",
]
