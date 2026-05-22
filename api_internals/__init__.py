"""Internal API helpers extracted from api.py.

Created by PR-H to shrink api.py's god-function. Each module here is
imported FROM api.py at create-app time; they are not part of the public
API. The single model bundle (`RealModelBundle`) is re-exported by
api.py for back-compat with tests + tools.
"""
