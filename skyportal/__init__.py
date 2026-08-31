"""Deprecated import shim for the pre-0.2.0 ``skyportal`` package.

The implementation moved to :mod:`skyportalai`. This shim keeps
``import skyportal`` working for one release and will be removed in 0.3.0.
"""

import warnings

from skyportalai._version import __version__

warnings.warn(
    "The 'skyportal' package is deprecated and will be removed in 0.3.0; "
    "import from 'skyportalai' instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["__version__"]
