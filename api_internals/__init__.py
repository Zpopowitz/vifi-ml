"""Internal API helpers extracted from api.py.

Created by PR-H to shrink api.py's god-function. Each module here is
imported FROM api.py at create-app time; they are not part of the
public API. To keep backwards compatibility for tests + tools that
do `from api import SyntheticModelBundle`, api.py re-exports.
"""
