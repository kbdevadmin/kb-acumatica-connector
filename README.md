# KB Data Connector — Acumatica MCP Server

A small, read-only server that lets Claude (yours or any teammate's) pull
live data directly from Kondor Blue's Acumatica instance: the backorder $
book (BackorderDollar&Units), Purchase Orders, and Inventory status —
without waiting on weekly file exports or emails.

Status as of 2026-09-22: code written and syntax/import-tested, but **not
yet tested against real Acumatica credentials** (deliberately — Claude
never handles your passwords or secrets). You'll do a short live test
yourself after deploying (Step 4 below).

---

## What's in this folder

- `server.py` — the connector itself (6 tools; see below)
- `requirements.txt` — Python dependencies
- `.env.example` — template for the environment variables it needs (copy to
  `.env` only for local testing — never commit a real `.env`)

## Tools this connector exposes

| Tool | Purpose |
|---|---|
| `acumatica_test_connection` | Run this first — confirms login works |
| `acumatica_get_backorder_dollar_units` | The $ book (open backorder revenue) — now pageable via `skip` |
| `acumatica_get_purchase_orders` | Open POs — vendor, Promised-On dates — now pageable via `skip` |
| `acumatica_get_inventory_status` | Inventory on-hand by SKU |
| `acumatica_get_po_line_details` | **New 2026-10-07.** Paginated PO line-level detail for one PO — use this instead of `acumatica_query_entity` + `expand=Details` for any PO with more than ~50 lines |
| `acumatica_discover_entity_fields` | **New 2026-10-07.** Returns just an entity's field names (tiny payload) — use this before guessing a field name |
| `acumatica_query_entity` | Advanced: query any Acumatica REST entity directly |
| `acumatica_query_odata_inquiry` | Advanced: query any Generic Inquiry exposed via OData — now pageable via `skip`, plus a `select` to narrow columns on wide/verbose GIs |

The last two are escape hatches — they let Claude explore exact field names
on your specific Acumatica instance rather than guessing, and give access to
anything not covered by the purpose-built tools above.

### 2026-10-07 fix — why some pulls were failing or getting cut off

In production use, pulls were either erroring with "exceeds maximum allowed
tokens" or returning wrong-looking numbers (e.g. a PO's receiving status).
Root causes, now fixed in `server.py`:

1. **Every row carried dead weight.** A PO's `note` field alone (boilerplate
   payment-terms text) could run 500+ characters, repeated on every line
   fetched with it; a `_links` block and an always-empty `custom: {}` added
   more on top. None of this is ever used for analysis. Fixed: every tool
   now strips these before returning anything — confirmed this alone cuts a
   95-line PO's payload by **~79%**.
2. **Responses were pretty-printed.** `indent=2` roughly doubles payload
   size for no benefit to a tool-calling LLM. Fixed: all responses are now
   compact JSON.
3. **No way to page past the first batch.** `acumatica_get_backorder_dollar_units`
   and `acumatica_get_purchase_orders` had no `skip` parameter, so a result
   set bigger than one response was simply unreachable. Fixed: both now take
   `skip`, same as the other tools.
4. **Acumatica's OData has no nested pagination.** `$expand=Details` on a
   PurchaseOrder always returns the WHOLE line-item array in one shot —
   there's no `Details($top=...)` support in this Acumatica version. For a
   PO with 80-95+ lines, that one-shot array still overflowed even after
   fix #1. Fixed: the new `acumatica_get_po_line_details` tool does the one
   unavoidable full fetch server-side, then slices the result in Python
   before ever serializing a response — so pulling a big PO's lines now
   means several small, safe calls (`skip=0`, `skip=25`, ...) instead of one
   that can't work at all.
5. **Guessing field names was expensive.** A wrong guess (e.g. `ReceiptQty`
   vs the real field name for PO line receiving quantity) 500s, and the only
   way to check was a full `expand=Details` pull — exactly the oversized
   call problem above. Fixed: `acumatica_discover_entity_fields` returns
   just the field names from one sample record (and one expanded
   sub-record), so field names can be verified cheaply before using them in
   `select`.

**You'll need to redeploy for these to take effect** — see "Updating the
deployment" below. No environment variables changed; this is a code-only
update.

### 2026-10-21 fix — `acumatica_query_odata_inquiry` had no `skip`

The two purpose-built pageable tools got `skip` in the 2026-10-07 fix above,
but the generic escape-hatch tool, `acumatica_query_odata_inquiry`, was
missed. This bit us for real: the custom GI built for location-level
inventory (`DB-StorageDetailsByItemWarehouseLocation`) has very verbose rows
(many duplicate/joined columns), and with no `skip` there was no way to get
past the first ~60-80 rows of any warehouse's data — meaning the
Incoming-Stock-Revenue pipeline could only ever see a partial slice of
inventory, not the full catalog, with no warning that it was partial.

Fixed: `acumatica_query_odata_inquiry` now takes `skip` (same pattern as the
other tools) and an optional `select` (comma-separated field names) to keep
only the columns actually needed — narrowing columns lets more rows fit in
one page before the output-size cap kicks in, which matters most for wide
GIs like this one. Page a large GI with `skip=0, top`, then `skip=top,
top=top`, etc., until a page returns fewer rows than `top`.

**You'll need to redeploy for this to take effect** — same as above, no
environment variables changed, code-only update.

---

## Deployment — Render.com (free tier, no server to manage)

**1. Create a Render account** at render.com if you don't have one
(free tier is enough to start).

**2. Get this code into a place Render can deploy from.** Easiest path:
create a new **private** GitHub repository (e.g. `kb-acumatica-connector`),
and upload these files to it — `server.py`, `requirements.txt`. (Skip
`.env.example` and never upload a real `.env`.) If you'd rather not use
GitHub, Render also supports deploying a zip via their dashboard — ask and
I can package one.

**3. In Render, create a new "Web Service"** pointing at that repo.
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python server.py`
- Instance type: Free is fine to start

**4. Set the environment variables** in Render's dashboard (Settings →
Environment) — this is where your real credentials go, never in the code
or in chat with me:

```
ACUMATICA_BASE_URL=https://kondorblue.acumatica.com
ACUMATICA_COMPANY=Kondor Blue - Production
ACUMATICA_CLIENT_ID=1EB88117-5321-340D-BFBF-AD02B9AEDDD6@Kondor Blue - Production
ACUMATICA_CLIENT_SECRET=<the shared secret you copied when creating KB Data Connector>
ACUMATICA_USERNAME=<your read-only API user's username>
ACUMATICA_PASSWORD=<your read-only API user's password>
```

**5. Deploy.** Render will give you a URL like
`https://kb-acumatica-connector.onrender.com`.

**6. Test it yourself first**, before connecting it to Claude — open
`https://<your-render-url>/health` or check Render's logs to confirm it
started without errors. If you have a way to call MCP tools directly (or
just connect it to your own Claude session first), run
`acumatica_test_connection` — it should return `"OK: authenticated
successfully..."`. If it errors, the message will say specifically what's
wrong (bad credentials, wrong API version, etc.) — send me that error text
(not the credentials) and I'll help fix it.

**7. Once it's confirmed working**, tell me the Render URL and I'll help
you register it as a connector in Claude/Cowork settings — for yourself
first, then for whichever teammates you decide should have it.

---

## Updating the deployment (code-only change, no new env vars)

Since this already runs on Render pointed at a GitHub repo:

1. Copy the updated `server.py` from this folder into your `kb-acumatica-connector`
   repo (overwrite the existing file), commit, and push to the branch Render
   deploys from.
2. Render should auto-deploy on push (if auto-deploy is on); otherwise open
   the service in Render's dashboard and click **Manual Deploy → Deploy
   latest commit**.
3. Watch the deploy logs for a clean startup (no import errors) — the tool
   list should show 8 tools now instead of 6 once it's back up.
4. From a Claude session with this connector attached, run
   `acumatica_test_connection` first, then try
   `acumatica_discover_entity_fields` on `PurchaseOrder` with
   `expand: "Details"` to confirm the new tools are live.
5. No `requirements.txt` changes needed — same dependencies as before.

If you're not using GitHub + auto-deploy (e.g. you uploaded a zip directly
to Render originally), just re-upload the updated `server.py` the same way
you did the first time.

## Status of the original open items (both resolved as of 2026-10-07)

1. ~~The OData endpoint path for BackorderDollar&Units~~ — confirmed working
   in production; no change needed.
2. ~~Inventory location-level detail~~ — confirmed the default `StockItem`
   entity pull works for item-level on-hand; a separate custom GI for full
   Warehouse+Location breakdown (PICK-LOCAT vs BACKORDER, and who BACKORDER
   stock is earmarked for) is still a nice-to-have but not blocking — the
   model currently gets this from the manual Inventory Balance export when
   needed.

## One open item remaining: PO line receiving-quantity field name

The model needs to know not just whether a PO line is `Completed`, but how
much of it has actually been received, to judge things like "is this PO
basically done, or is completion lagging behind real receiving?" Two
guessed field names for this (`ReceivedQty`, `ReceiptQty`) both failed with
500 errors against this instance. Next time you're testing:

```
acumatica_discover_entity_fields(entity_name="PurchaseOrder", filter_odata="OrderNbr eq '<any open PO>'", expand="Details")
```

will return the exact field names Acumatica uses on a Details line for this
instance — look for whatever it's actually called (could be `ReceivedQtyEntered`,  `QtyReceived`, or something Acumatica-version-specific), then that field name can be added to `acumatica_get_po_line_details`'s default `select`, or just passed directly in `select` when calling it.

## Security notes

- The API user this connector logs in as should have **read-only** access
  to Sales Orders, Purchase Orders, and Inventory only — nothing else.
- The Connected Application's shared secret is set to expire in 30 days
  (your choice) — you'll need to add a new secret and update the Render
  environment variable before then, or it'll stop authenticating.
- Anyone with access to this connector (once shared with teammates) can see
  everything it's scoped to read — treat the list of who has it deliberately.
