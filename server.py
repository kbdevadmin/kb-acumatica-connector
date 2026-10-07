#!/usr/bin/env python3
"""
MCP Server for Kondor Blue's Acumatica ERP instance.

Exposes read-only tools for the three data feeds the Incoming-Stock-Revenue
model (and other KB reporting) depends on:
  - BackorderDollar&Units  (a Generic Inquiry exposed via OData — the $ book)
  - Purchase Orders        (native Acumatica REST entity)
  - Inventory / Stock      (native Acumatica REST entity)

Plus two general-purpose tools (query + metadata discovery) so any entity or
Generic Inquiry can be reached even if it isn't one of the three built-ins
above, and so field names can be verified live against this specific
Acumatica instance rather than assumed.

Authentication: OAuth 2.0 "Resource Owner Password Credentials" (ROPC) flow,
matching the "KB Data Connector" Connected Application already registered in
Acumatica (System > Integration > Connected Applications, SM303010). Nothing
in this file contains real credentials — everything sensitive is read from
environment variables at runtime. See .env.example.

Kondor Blue-specific notes (see project memory "acumatica-auto-pull" for the
full history):
  - BackorderDollar&Units is a Generic Inquiry, Screen ID KB.00.00.01, built
    on PX.Objects.SO.SOLine + SOOrder, with "Expose via OData" checked
    (done 2026-09-22). GIs exposed this way are served through Acumatica's
    OData endpoint, NOT the contract-based /entity/ endpoint — see
    ODATA_BASE_PATH below and acumatica_discover_odata_entities().
  - Purchase Orders and Inventory are NOT custom Generic Inquiries — they
    come from Acumatica's native screens (Purchase Orders / Inventory
    Summary), so they're queried through the standard contract-based REST
    API (/entity/Default/<version>/...) instead.
  - Inventory location-level detail (PICK-LOCAT vs BACKORDER, and within
    BACKORDER who it's earmarked for — B&H/RESELLER/CUSTOMER) is required
    by the KB model. It is NOT yet confirmed whether the default Acumatica
    inventory entity returns this location breakdown or only item-level
    totals. Use acumatica_discover_entity_fields("StockItem") (or
    whichever entity name this instance uses) to check the available
    fields before assuming — if location detail is missing, a small custom
    Generic Inquiry (mirroring the BackorderDollar&Units approach) will be
    needed for that one field.

CHANGELOG (2026-10-07) — fixes for the size-cap / pagination problems found
in production use (see project memory "backorder-blocker-report-2026-10-07-
handoff"): every tool's raw JSON response was blowing past the MCP client's
~25-60K-char tool-output limit well before Acumatica's own row limits, for
two reasons that had nothing to do with row count:
  1. Every row carried dead weight — a verbose "note" field (boilerplate PO
     payment-terms text, often 500+ chars, repeated per PO), a "_links"
     block (self/files:put URLs, ~150+ chars per row), and an always-empty
     "custom": {} — none of which the model ever uses.
  2. Responses were pretty-printed with indent=2, roughly doubling payload
     size for no benefit to a tool-calling LLM.
  3. BackorderDollarUnitsInput and PurchaseOrdersInput had no `skip` param,
     so there was no way to page through a result set larger than one
     response — `top` alone always returns the same first N rows.
  4. There was no cheap way to verify an entity's real field names before
     guessing (e.g. guessing "ReceiptQty"/"ReceivedQty" for PO line receiving
     quantity both 500'd) — doing it the only available way (a full
     expand=Details pull) burned a huge, size-capped call just to see field
     names.
  5. OData `$expand=Details` on PurchaseOrder has no working nested
     pagination in this Acumatica version (`Details($top=...)` 500s), so a
     PO with 80-95+ lines (e.g. PO 001265) could never be pulled in one
     piece — the whole Details array comes back in one shot or not at all.
Fixes applied below: `_strip_noise()` strips note/_links/custom before
anything is returned; JSON is now compact (no indent); `skip` was added to
every paginatable input; a new `acumatica_discover_entity_fields` tool
returns just field names (tiny payload, no guessing); and a new
`acumatica_get_po_line_details` tool does the one unavoidable full
expand=Details fetch server-side, then slices the Details list in Python
before returning — giving real pagination over PO lines even though
Acumatica's own OData doesn't support it natively.
"""

import os
import time
import json
import logging
from typing import Optional, Any
from enum import Enum

import httpx
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("acumatica_mcp")

# ---------------------------------------------------------------------------
# Configuration (all from environment — never hardcode secrets here)
# ---------------------------------------------------------------------------

ACUMATICA_BASE_URL = os.environ.get("ACUMATICA_BASE_URL", "").rstrip("/")
# e.g. "https://kondorblue.acumatica.com"

ACUMATICA_COMPANY = os.environ.get("ACUMATICA_COMPANY", "Kondor Blue - Production")
ACUMATICA_CLIENT_ID = os.environ.get("ACUMATICA_CLIENT_ID", "")
ACUMATICA_CLIENT_SECRET = os.environ.get("ACUMATICA_CLIENT_SECRET", "")
ACUMATICA_USERNAME = os.environ.get("ACUMATICA_USERNAME", "")
ACUMATICA_PASSWORD = os.environ.get("ACUMATICA_PASSWORD", "")

# Contract-based REST API endpoint version. Confirm this matches the
# instance (Help > About in Acumatica, or try "24.200.001" style versions
# until one responds) — 24.200.001 is a reasonable current default.
ACUMATICA_API_VERSION = os.environ.get("ACUMATICA_API_VERSION", "24.200.001")

# Generic Inquiries exposed "via OData" are served from a separate OData
# endpoint, not /entity/. The exact path has historically been one of:
#   /odata/<Company>/<InquiryName>
#   /entity/OData/<InquiryName>
# Kept configurable since it varies by Acumatica version — verify with
# acumatica_discover_odata_entities() once credentials are live, and update
# this env var / default if needed.
ODATA_BASE_PATH = os.environ.get("ACUMATICA_ODATA_BASE_PATH", "/odata")

TOKEN_URL_PATH = "/identity/connect/token"
ENTITY_BASE_PATH = f"/entity/Default/{ACUMATICA_API_VERSION}"

REQUEST_TIMEOUT = 30.0

mcp = FastMCP("acumatica_mcp")

# ---------------------------------------------------------------------------
# Auth: OAuth 2.0 Resource Owner Password Credentials, with simple in-memory
# token caching (a fresh process — e.g. a Render restart — just re-logs in).
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0}


def _require_config() -> Optional[str]:
    """Returns an error string if required config is missing, else None."""
    missing = [
        name
        for name, val in [
            ("ACUMATICA_BASE_URL", ACUMATICA_BASE_URL),
            ("ACUMATICA_CLIENT_ID", ACUMATICA_CLIENT_ID),
            ("ACUMATICA_CLIENT_SECRET", ACUMATICA_CLIENT_SECRET),
            ("ACUMATICA_USERNAME", ACUMATICA_USERNAME),
            ("ACUMATICA_PASSWORD", ACUMATICA_PASSWORD),
        ]
        if not val
    ]
    if missing:
        return (
            "Error: missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set these on the hosting platform (never in code or chat)."
        )
    return None


async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Fetch (or reuse a cached) OAuth access token via the ROPC flow."""
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 30:
        return _token_cache["access_token"]

    resp = await client.post(
        f"{ACUMATICA_BASE_URL}{TOKEN_URL_PATH}",
        data={
            "grant_type": "password",
            "username": ACUMATICA_USERNAME,
            "password": ACUMATICA_PASSWORD,
            "client_id": ACUMATICA_CLIENT_ID,
            "client_secret": ACUMATICA_CLIENT_SECRET,
            "scope": "api offline_access",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    logger.info("Acumatica: obtained new access token, expires in %ss", data.get("expires_in"))
    return _token_cache["access_token"]


async def _authed_client() -> httpx.AsyncClient:
    client = httpx.AsyncClient()
    token = await _get_access_token(client)
    client.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    )
    return client


def _handle_api_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body_snippet = e.response.text[:500]
        if status == 401:
            return (
                "Error: Acumatica authentication failed (401). Check "
                "ACUMATICA_CLIENT_ID / ACUMATICA_CLIENT_SECRET / "
                "ACUMATICA_USERNAME / ACUMATICA_PASSWORD env vars, and "
                "confirm the 'KB Data Connector' Connected Application in "
                "Acumatica is still Active with a non-expired shared secret."
            )
        if status == 403:
            return (
                "Error: Acumatica permission denied (403). The API user's "
                "role may not have read access to this screen/entity."
            )
        if status == 404:
            return (
                f"Error: Acumatica endpoint not found (404). The entity or "
                f"inquiry name, or the API version path ({ACUMATICA_API_VERSION}), "
                f"may be wrong for this instance. Response snippet: {body_snippet}"
            )
        if status == 429:
            return "Error: Acumatica rate limit exceeded. Wait and retry."
        return f"Error: Acumatica API request failed ({status}). Response: {body_snippet}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to Acumatica timed out. Please retry."
    return f"Error: Unexpected error calling Acumatica: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Shared request helpers
# ---------------------------------------------------------------------------

async def _entity_get(path: str, params: Optional[dict] = None) -> Any:
    """GET against the contract-based REST entity endpoint (/entity/Default/<ver>/...)."""
    async with await _authed_client() as client:
        url = f"{ACUMATICA_BASE_URL}{ENTITY_BASE_PATH}/{path.lstrip('/')}"
        resp = await client.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()


async def _odata_get(path: str, params: Optional[dict] = None) -> Any:
    """GET against the OData endpoint used for GIs exposed 'via OData'."""
    async with await _authed_client() as client:
        url = f"{ACUMATICA_BASE_URL}{ODATA_BASE_PATH}/{path.lstrip('/')}"
        resp = await client.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        # OData JSON responses often wrap results in a "value" array.
        data = resp.json()
        return data


def _company_qs() -> dict:
    return {"$company": ACUMATICA_COMPANY} if ACUMATICA_COMPANY else {}


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    JSON = "json"
    MARKDOWN = "markdown"


class BackorderDollarUnitsInput(BaseModel):
    """Input for pulling the BackorderDollar&Units report (the $ book)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    top: int = Field(
        default=150,
        description="Max number of rows to return (OData $top). Keep this at "
        "150 or below per call — the full backorder book runs to ~1,400+ "
        "lines, and rows are verbose enough that larger pages routinely blow "
        "past the tool-output size cap even after noise-stripping. Page "
        "through the full book with `skip` instead of raising this.",
        ge=1,
        le=10000,
    )
    skip: int = Field(
        default=0,
        description="OData $skip — how many matching rows to skip before "
        "returning `top` more. Use this to page through the full book in "
        "~150-row chunks (skip=0, then skip=150, skip=300, ...) since a "
        "single call can't safely return it all at once.",
        ge=0,
    )
    filter_odata: Optional[str] = Field(
        default=None,
        description="Optional raw OData $filter expression, e.g. "
        "\"UnbilledAmount gt 0\" to restrict to lines with real unbilled $ — "
        "mirrors the model's 'q>0 and amt>0' open-backorder rule. Leave blank "
        "to get all rows and filter client-side instead.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


class PurchaseOrdersInput(BaseModel):
    """Input for pulling open Purchase Orders (Promised-On dates, vendor, status)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status_filter: Optional[str] = Field(
        default="Open",
        description="Purchase order Status to filter on, e.g. 'Open'. Pass null/empty "
        "to fetch all statuses.",
    )
    top: int = Field(default=200, ge=1, le=10000)
    skip: int = Field(
        default=0,
        description="OData $skip, for paging past the first `top` POs if there "
        "are more open POs than fit in one response.",
        ge=0,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


class InventoryStatusInput(BaseModel):
    """Input for pulling inventory on-hand quantities."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    inventory_id: Optional[str] = Field(
        default=None,
        description="Optional single SKU (Acumatica Inventory ID) to look up, "
        "e.g. 'KB-MAGICARM-CC'. Leave blank to pull all items (large — prefer "
        "paging with top/skip for full-catalog pulls).",
    )
    top: int = Field(default=500, ge=1, le=10000)
    skip: int = Field(default=0, ge=0)
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


class RawEntityQueryInput(BaseModel):
    """Generic escape hatch: query any contract-based REST entity directly.

    Use this when one of the three purpose-built tools above doesn't cover
    what's needed, or to verify exact field names/values before hardcoding
    assumptions about this Acumatica instance.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    entity_name: str = Field(
        ...,
        description="Contract-based API entity name, e.g. 'SalesOrder', "
        "'PurchaseOrder', 'StockItem', 'Vendor'.",
        min_length=1,
    )
    select: Optional[str] = Field(
        default=None, description="Optional OData $select, comma-separated field list."
    )
    filter_odata: Optional[str] = Field(
        default=None, description="Optional OData $filter expression."
    )
    expand: Optional[str] = Field(
        default=None, description="Optional OData $expand for related sub-entities."
    )
    top: int = Field(default=100, ge=1, le=10000)
    skip: int = Field(default=0, ge=0)


class RawODataQueryInput(BaseModel):
    """Generic escape hatch for Generic Inquiries exposed 'via OData'
    (like BackorderDollar&Units), by exact inquiry name.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    inquiry_name: str = Field(
        ...,
        description="Exact Generic Inquiry title as shown in Acumatica, e.g. "
        "'BackorderDollar&Units'.",
        min_length=1,
    )
    filter_odata: Optional[str] = Field(default=None, description="Optional OData $filter.")
    top: int = Field(default=500, ge=1, le=10000)


class DiscoverEntityFieldsInput(BaseModel):
    """Input for discovering an entity's real field names cheaply."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    entity_name: str = Field(
        ...,
        description="Contract-based API entity name, e.g. 'PurchaseOrder', 'StockItem'.",
        min_length=1,
    )
    filter_odata: Optional[str] = Field(
        default=None,
        description="Optional OData $filter to target a specific record (e.g. "
        "\"OrderNbr eq '001265'\") so the sample row is representative.",
    )
    expand: Optional[str] = Field(
        default=None,
        description="Optional OData $expand, e.g. 'Details', to also discover "
        "the field names of a nested sub-collection (its first item only).",
    )


class POLineDetailsInput(BaseModel):
    """Input for paginated PO line-level detail — works around Acumatica's
    lack of nested pagination on PurchaseOrder/Details."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    order_nbr: str = Field(
        ..., description="The PO's OrderNbr, e.g. '001265'.", min_length=1
    )
    top: int = Field(
        default=25,
        description="Max number of Details lines to return in this page. "
        "Keep this modest (20-30) — large POs (80-95+ lines) still need "
        "several pages even after noise-stripping.",
        ge=1,
        le=500,
    )
    skip: int = Field(
        default=0,
        description="How many Details lines to skip before returning `top` "
        "more. Page through a large PO with skip=0, then skip=25, skip=50, ...",
        ge=0,
    )
    select: Optional[str] = Field(
        default=None,
        description="Optional comma-separated list of Details field names to "
        "keep (e.g. 'InventoryID,OrderQty,Completed'). If omitted, returns "
        "all fields on each line (after noise-stripping) — narrowing this "
        "reduces payload size further, which matters on large POs.",
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _rows_from_odata_payload(payload: Any) -> list:
    if isinstance(payload, dict) and "value" in payload:
        return payload["value"]
    if isinstance(payload, list):
        return payload
    return [payload]


# Fields that are pure overhead for this model's purposes — never useful for
# analysis, and often the single biggest contributor to a response blowing
# past the tool-output size cap (a PO's "note" field alone can run 500+
# chars of boilerplate payment-terms text, repeated on every line).
_NOISE_FIELDS = {"note", "_links", "custom"}


def _strip_noise(obj: Any) -> Any:
    """Recursively drop noisy/useless fields from an Acumatica row (and any
    nested sub-collections, e.g. PurchaseOrder.Details) before it's ever
    serialized. This is the main lever for staying under the tool-output
    size cap without reducing row counts."""
    if isinstance(obj, dict):
        return {
            k: _strip_noise(v)
            for k, v in obj.items()
            if k not in _NOISE_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_noise(item) for item in obj]
    return obj


def _dumps(payload: Any) -> str:
    """Compact JSON (no pretty-print indent) — indent=2 roughly doubles
    payload size for no benefit to a tool-calling LLM, and every byte here
    counts against the tool-output size cap."""
    return json.dumps(payload, separators=(",", ":"), default=str)


def _to_markdown_table(rows: list, max_rows: int = 50) -> str:
    if not rows:
        return "_No rows returned._"
    keys = list(rows[0].keys()) if isinstance(rows[0], dict) else []
    lines = [f"Returned {len(rows)} row(s), showing up to {max_rows}:", ""]
    if keys:
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("| " + " | ".join(["---"] * len(keys)) + " |")
        for row in rows[:max_rows]:
            lines.append("| " + " | ".join(str(row.get(k, "")) for k in keys) + " |")
    else:
        for row in rows[:max_rows]:
            lines.append(f"- {row}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="acumatica_get_backorder_dollar_units",
    annotations={
        "title": "Get Kondor Blue Backorder $ Book (BackorderDollar&Units)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_get_backorder_dollar_units(params: BackorderDollarUnitsInput) -> str:
    """Pull the BackorderDollar&Units Generic Inquiry — Kondor Blue's authoritative
    open-backorder dollar book (Inventory ID, Customer, Open Qty, Amount,
    Unbilled Amount, status), exposed via Acumatica OData.

    This is THE $ source for the weekly Incoming-Stock-Revenue model and any
    other KB reporting that needs open backorder dollars. Per KB's standing
    rule, "revenue coming in" = Unbilled Amount on lines with Open Qty > 0 and
    Amount > 0 — this tool returns raw rows; apply that filter client-side
    (or via filter_odata) as needed.

    Args:
        params (BackorderDollarUnitsInput): top (max rows), optional
            filter_odata (raw OData $filter), response_format (json|markdown).

    Returns:
        str: JSON array of backorder line objects (or a markdown table if
        response_format="markdown"). Each row is expected to include fields
        such as InventoryID, CustomerID, OpenQty, Amount, UnbilledAmount —
        exact field names depend on the GI's configured columns; if a field
        you expect is missing, use acumatica_discover_odata_entities or
        acumatica_query_odata_inquiry to inspect the raw shape.

    Error Handling:
        Returns "Error: ..." with a specific, actionable message on auth
        failure, permission issues, or if the OData endpoint path for this
        GI doesn't match ACUMATICA_ODATA_BASE_PATH's default assumption.
    """
    err = _require_config()
    if err:
        return err
    try:
        params_qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter_odata:
            params_qs["$filter"] = params.filter_odata
        payload = await _odata_get("BackorderDollar&Units", params=params_qs)
        rows = _strip_noise(_rows_from_odata_payload(payload))
        if params.response_format == ResponseFormat.MARKDOWN:
            return _to_markdown_table(rows)
        return _dumps({"count": len(rows), "skip": params.skip, "rows": rows})
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_get_purchase_orders",
    annotations={
        "title": "Get Kondor Blue Purchase Orders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_get_purchase_orders(params: PurchaseOrdersInput) -> str:
    """Pull Purchase Orders from Acumatica's native PurchaseOrder entity —
    the source for PO timing (Promised-On date), vendor, and status that the
    Incoming-Stock-Revenue model needs (equivalent to KB's old 'ALL POS
    Acumatica' manual export).

    Args:
        params (PurchaseOrdersInput): status_filter (default 'Open'; pass
            null for all statuses), top (max rows), response_format.

    Returns:
        str: JSON array of purchase order objects (or markdown table).
        Expected fields include OrderNbr, VendorID, VendorRef, Status,
        Date, PromisedOn (naming may vary slightly by Acumatica version —
        verify with acumatica_query_entity(entity_name="PurchaseOrder",
        top=1) if a field isn't where expected).

    Error Handling:
        Returns "Error: ..." with actionable detail on auth/permission/404
        (e.g. if ACUMATICA_API_VERSION doesn't match this instance).
    """
    err = _require_config()
    if err:
        return err
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.status_filter:
            qs["$filter"] = f"Status eq '{params.status_filter}'"
        payload = await _entity_get("PurchaseOrder", params=qs)
        rows = _strip_noise(_rows_from_odata_payload(payload))
        if params.response_format == ResponseFormat.MARKDOWN:
            return _to_markdown_table(rows)
        return _dumps({"count": len(rows), "skip": params.skip, "rows": rows})
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_get_inventory_status",
    annotations={
        "title": "Get Kondor Blue Inventory On-Hand Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_get_inventory_status(params: InventoryStatusInput) -> str:
    """Pull inventory on-hand data from Acumatica's native StockItem-family
    entity — equivalent to KB's old 'Inventory Balance' manual export.

    IMPORTANT / UNVERIFIED: the KB model needs location-level detail
    (Warehouse + Location, e.g. PICK-LOCAT vs BACKORDER, and within
    BACKORDER whether stock is earmarked B&H/RESELLER/CUSTOMER). It has not
    yet been confirmed whether this default entity returns that breakdown or
    only item-level totals. If location fields are missing from the
    response, that detail will need a small custom Generic Inquiry (same
    OData-exposure approach used for BackorderDollar&Units) rather than this
    tool.

    Args:
        params (InventoryStatusInput): inventory_id (optional single SKU),
            top/skip (paging), response_format.

    Returns:
        str: JSON array of inventory item objects (or markdown table).

    Error Handling:
        Returns "Error: ..." with actionable detail on auth/permission/404.
    """
    err = _require_config()
    if err:
        return err
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.inventory_id:
            qs["$filter"] = f"InventoryID eq '{params.inventory_id}'"
        payload = await _entity_get("StockItem", params=qs)
        rows = _strip_noise(_rows_from_odata_payload(payload))
        if params.response_format == ResponseFormat.MARKDOWN:
            return _to_markdown_table(rows)
        return _dumps({"count": len(rows), "skip": params.skip, "rows": rows})
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_query_entity",
    annotations={
        "title": "Query Any Acumatica REST Entity (Advanced)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_query_entity(params: RawEntityQueryInput) -> str:
    """General-purpose, read-only query against any Acumatica contract-based
    REST API entity (e.g. SalesOrder, PurchaseOrder, StockItem, Vendor,
    Customer). Use this to explore field names on this specific Acumatica
    instance, or to reach data not covered by the three purpose-built tools.

    Does NOT create, update, or delete anything — GET requests only.

    Args:
        params (RawEntityQueryInput): entity_name (required), select, filter_odata,
            expand, top, skip.

    Returns:
        str: JSON object {"count": int, "rows": [...]} with the raw entity
        rows as returned by Acumatica.

    Error Handling:
        Returns "Error: ..." — a 404 usually means the entity name or
        ACUMATICA_API_VERSION doesn't match this instance; try
        acumatica_query_entity with a well-known entity like "Customer"
        first to confirm connectivity before assuming a specific name is
        wrong.
    """
    err = _require_config()
    if err:
        return err
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.select:
            qs["$select"] = params.select
        if params.filter_odata:
            qs["$filter"] = params.filter_odata
        if params.expand:
            qs["$expand"] = params.expand
        payload = await _entity_get(params.entity_name, params=qs)
        rows = _strip_noise(_rows_from_odata_payload(payload))
        return _dumps({"count": len(rows), "skip": params.skip, "rows": rows})
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_query_odata_inquiry",
    annotations={
        "title": "Query Any Acumatica Generic Inquiry Exposed via OData (Advanced)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_query_odata_inquiry(params: RawODataQueryInput) -> str:
    """General-purpose, read-only query against any Generic Inquiry that has
    been exposed 'via OData' in Acumatica (Generic Inquiry screen SM208000,
    Interface Options tab). Use this for any future custom report beyond
    BackorderDollar&Units — for example, if a small custom GI ends up being
    built for location-level inventory detail.

    Args:
        params (RawODataQueryInput): inquiry_name (exact title as shown in
            Acumatica), filter_odata, top.

    Returns:
        str: JSON object {"count": int, "rows": [...]}.

    Error Handling:
        Returns "Error: ..." — a 404 likely means either the inquiry name is
        wrong/not exposed via OData yet, or ACUMATICA_ODATA_BASE_PATH needs
        adjusting for this Acumatica version (see module docstring).
    """
    err = _require_config()
    if err:
        return err
    try:
        qs: dict = {"$top": params.top}
        if params.filter_odata:
            qs["$filter"] = params.filter_odata
        payload = await _odata_get(params.inquiry_name, params=qs)
        rows = _strip_noise(_rows_from_odata_payload(payload))
        return _dumps({"count": len(rows), "rows": rows})
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_discover_entity_fields",
    annotations={
        "title": "Discover an Entity's Real Field Names (cheap, no guessing)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_discover_entity_fields(params: DiscoverEntityFieldsInput) -> str:
    """Fetch exactly one record of an entity and return ONLY its field names
    (not the values) — plus the field names of one expanded sub-collection
    item if `expand` is given. Tiny payload, so it never hits the size cap.

    Use this BEFORE guessing a field name for acumatica_query_entity's
    `select` (e.g. "is it ReceivedQty or ReceiptQty on a PO line?") — a wrong
    guess 500s, and discovering it via a full expand=Details pull burns a
    huge call just to see field names. This tool solves that cheaply.

    Args:
        params (DiscoverEntityFieldsInput): entity_name, optional filter_odata
            to target a specific record, optional expand (e.g. "Details").

    Returns:
        str: JSON object like {"fields": [...], "expand_fields": {"Details": [...]}}.

    Error Handling:
        Returns "Error: ..." with the same actionable detail as the other
        tools on auth/permission/404.
    """
    err = _require_config()
    if err:
        return err
    try:
        qs: dict = {"$top": 1}
        if params.filter_odata:
            qs["$filter"] = params.filter_odata
        if params.expand:
            qs["$expand"] = params.expand
        payload = await _entity_get(params.entity_name, params=qs)
        rows = _rows_from_odata_payload(payload)
        if not rows:
            return _dumps({"fields": [], "note": "No rows matched — can't discover fields from zero rows."})
        sample = rows[0]
        result: dict = {"fields": sorted(sample.keys())}
        if params.expand:
            expand_result = {}
            for expand_key in params.expand.split(","):
                expand_key = expand_key.strip()
                nested = sample.get(expand_key)
                if isinstance(nested, list) and nested:
                    expand_result[expand_key] = sorted(nested[0].keys())
                elif isinstance(nested, dict):
                    expand_result[expand_key] = sorted(nested.keys())
            result["expand_fields"] = expand_result
        return _dumps(result)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_get_po_line_details",
    annotations={
        "title": "Get Paginated PO Line Details (works around no nested $top/$skip)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_get_po_line_details(params: POLineDetailsInput) -> str:
    """Get one PO's line-level Details, paginated — works around the fact
    that this Acumatica version's OData does not support nested pagination
    on PurchaseOrder/Details (`Details($top=...)` 500s, and omitting it
    returns the WHOLE Details array in one shot, which overflows the
    tool-output size cap for any PO with more than ~50-60 lines, e.g. PO
    001265's 95 lines).

    This tool does the one unavoidable full expand=Details fetch against
    Acumatica (there's no way around that — Acumatica itself doesn't let us
    ask for a slice), then slices the Details list in Python using
    `skip`/`top` before ever serializing a response, so what comes back to
    the caller is always a small, safe page regardless of how large the PO
    is. Call it repeatedly with increasing `skip` to walk the whole PO.

    Args:
        params (POLineDetailsInput): order_nbr, top, skip, optional select
            (comma-separated Details field names to keep).

    Returns:
        str: JSON object {"order_nbr": ..., "total_lines": int, "skip": int,
        "returned": int, "lines": [...]}. `total_lines` lets the caller know
        when to stop paging (skip + returned >= total_lines).

    Error Handling:
        Returns "Error: ..." with the same actionable detail as the other
        tools on auth/permission/404. A 404/empty result usually means the
        OrderNbr doesn't exist or isn't visible to the API user's role.
    """
    err = _require_config()
    if err:
        return err
    try:
        qs: dict = {
            "$filter": f"OrderNbr eq '{params.order_nbr}'",
            "$expand": "Details",
            "$top": 1,
        }
        payload = await _entity_get("PurchaseOrder", params=qs)
        rows = _rows_from_odata_payload(payload)
        if not rows:
            return _dumps({"error": f"No PurchaseOrder found with OrderNbr '{params.order_nbr}'."})
        details = rows[0].get("Details") or []
        total_lines = len(details)
        page = details[params.skip : params.skip + params.top]
        page = _strip_noise(page)
        if params.select:
            keep = {f.strip() for f in params.select.split(",")}
            page = [{k: v for k, v in line.items() if k in keep} for line in page]
        return _dumps(
            {
                "order_nbr": params.order_nbr,
                "total_lines": total_lines,
                "skip": params.skip,
                "returned": len(page),
                "lines": page,
            }
        )
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@mcp.tool(
    name="acumatica_test_connection",
    annotations={
        "title": "Test Acumatica Connection & Auth",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def acumatica_test_connection() -> str:
    """Verify the connector can authenticate to Acumatica and reach the
    contract-based REST API. Does not touch any business data beyond
    fetching a single well-known entity (Customer) as a connectivity probe.

    Run this FIRST after deploying/configuring the connector, before relying
    on any other tool, to confirm environment variables and the Connected
    Application are set up correctly.

    Returns:
        str: "OK: ..." with the resolved base URL and API version on
        success, or "Error: ..." with specific guidance on what's
        misconfigured (auth, permissions, wrong API version, etc).
    """
    err = _require_config()
    if err:
        return err
    try:
        async with await _authed_client() as client:
            url = f"{ACUMATICA_BASE_URL}{ENTITY_BASE_PATH}/Customer"
            resp = await client.get(url, params={"$top": 1}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        return (
            f"OK: authenticated successfully to {ACUMATICA_BASE_URL} "
            f"(API version {ACUMATICA_API_VERSION}, company "
            f"'{ACUMATICA_COMPANY}'). Connector is ready."
        )
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


if __name__ == "__main__":
    # Streamable HTTP transport so this can run as a remote-hosted connector
    # (e.g. on Render) that any teammate's Claude session can reach.
    # NOTE: in this mcp SDK version (<2.0.0), host/port are set via
    # mcp.settings, not as kwargs to run() — and the transport string uses
    # a hyphen ("streamable-http"), not an underscore.
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="streamable-http")
