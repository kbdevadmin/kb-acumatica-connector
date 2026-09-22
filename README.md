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
| `acumatica_get_backorder_dollar_units` | The $ book (open backorder revenue) |
| `acumatica_get_purchase_orders` | Open POs — vendor, Promised-On dates |
| `acumatica_get_inventory_status` | Inventory on-hand by SKU |
| `acumatica_query_entity` | Advanced: query any Acumatica REST entity directly |
| `acumatica_query_odata_inquiry` | Advanced: query any Generic Inquiry exposed via OData |

The last two are escape hatches — they let Claude explore exact field names
on your specific Acumatica instance rather than guessing, and give access to
anything not covered by the three purpose-built tools above.

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

## Two open items to resolve once you're testing with real data

1. **The OData endpoint path for BackorderDollar&Units.** Acumatica's exact
   URL pattern for Generic Inquiries exposed "via OData" varies by version.
   The code defaults to `/odata/BackorderDollar&Units` — if
   `acumatica_get_backorder_dollar_units` 404s, try
   `acumatica_query_odata_inquiry` with a few path variations, or check
   Acumatica's own documentation/Swagger page for this instance
   (`https://kondorblue.acumatica.com/entity/swagger` is worth checking).

2. **Inventory location-level detail.** The model needs to know not just
   total on-hand per SKU but which warehouse/location it's sitting in
   (PICK-LOCAT vs. BACKORDER, and within BACKORDER whether it's earmarked
   B&H/Reseller/Customer). It's not yet confirmed whether Acumatica's
   default `StockItem` entity includes that breakdown. Use
   `acumatica_query_entity` with `entity_name="StockItem"` once it's live
   to check — if location detail is missing, we'll likely need one more
   small Generic Inquiry (same "Expose via OData" approach used for
   BackorderDollar&Units) built specifically for that.

## Security notes

- The API user this connector logs in as should have **read-only** access
  to Sales Orders, Purchase Orders, and Inventory only — nothing else.
- The Connected Application's shared secret is set to expire in 30 days
  (your choice) — you'll need to add a new secret and update the Render
  environment variable before then, or it'll stop authenticating.
- Anyone with access to this connector (once shared with teammates) can see
  everything it's scoped to read — treat the list of who has it deliberately.
