"""Background QgsTask wrapper around :func:`search.search_catalog`.

Kept apart so ``search.py`` stays stdlib-only and testable without QGIS.
"""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

from qgis.core import QgsTask

from .auth import request_headers
from .net import StacError
from .search import search_catalog, server_cloud_filter

if TYPE_CHECKING:
    from .catalogs import CatalogProvider
    from .items import StacItemResult
    from .search import PageToken

__all__ = ["StacSearchTask"]


class StacSearchTask(QgsTask):
    """Background :func:`search_catalog` call.

    The *catalog*'s headers (static + QGIS auth config) are resolved in the
    background thread, so an OAuth2 token fetch never blocks the UI. Every
    other keyword goes straight to :func:`search_catalog`, except that
    ``server_side_cloud_filter`` is narrowed by :func:`server_cloud_filter`.
    """

    def __init__(self, catalog: CatalogProvider, **search_kwargs):
        desc = (
            "Searching more…"
            if search_kwargs.get("page_token")
            else f"Searching {search_kwargs['collection']}"
        )
        super().__init__(desc)
        self.catalog = catalog
        search_kwargs["server_side_cloud_filter"] = search_kwargs.get(
            "server_side_cloud_filter", False
        ) and server_cloud_filter(catalog, search_kwargs["collection"])
        self.search_kwargs = search_kwargs
        self.results: list[StacItemResult] = []
        self.next_page: PageToken | None = None
        self.error: str | None = None
        self.error_kind: str | None = None

    def run(self) -> bool:
        try:
            self.results, self.next_page = search_catalog(
                **self.search_kwargs,
                auth_headers=request_headers(self.catalog) or None,
                cancel_check=self.isCanceled,
            )
            return True
        except Exception as e:
            self.error_kind = e.kind if isinstance(e, StacError) else "unknown"
            self.error = f"{e}\n{traceback.format_exc()}"
            return False
