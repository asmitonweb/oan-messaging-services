# Beneficiary API: what the farmer registry needs to provide

**For:** the farmer registry / DPI common services developers
**From:** the OAN messaging team, 20 November 2026
**Contract:** [`beneficiary-api.openapi.yaml`](beneficiary-api.openapi.yaml)
**Spec:** Channels Specification v2.0, §8.4 items ID-01 (one farmer by Farmer ID) and ID-04
(Master Data kebele code on each farmer)

## Why

The DA web app sends broadcasts to **all farmers** of a region, zone or woreda (the
Communications Officer). The SMS copy needs each farmer's **registered phone**. The Telegram
bot needs, for one Farmer ID, the registered phone, kebele and name to link a farmer. Both
must come from the farmer registry, through one service-to-service API. That way nothing
else keeps a second copy of farmer phones.

## What exists today (checked 20 November 2026)

| Route | Why it does not serve this |
|---|---|
| `iam-bene-portal-api` (`bene-iam.development.oanstaging.com`) | Farmer **login** (eSignet/Fayda, session cookie). Holds no farmer data |
| Registry bene-portal API (`openg2p-registry-bene-api`) | Disabled on Dev (`enabled: false`); upstream it returns register metadata only |
| Aggregation layer Beneficiary-360, partner `/dci/registry/sync/search` | One farmer per call, keyed by Fayda number, with that **farmer's consent** on every call. Unworkable for a broadcast |
| Staff API `POST /register-data/search_in_a_register` (used by the DA backend and A2C) | Filters on **names** only (`region_name` … `kebele_name`, text `eq`/`contains`). Names are not unique. The filter list is at its limit of 10 fields (`farmer-registry` `05_registers.sql`). Rows carry `primary_phone_number` but no area codes and no language. On Dev on 29 Oct it ignored `filter_by`, so the DA backend reads everything and filters itself |
| WebSub `farmer-registry.farmer-profile.changed.v1` | Carries the Farmer ID only, no area. No full load |

The registry already stores what is needed:
- the kebele as a Master Data id (`geo_lowest_level_value_id`);
- the full code chain (`geo_code_hierarchy_json`);
- phones (`phone_numbers[{number, is_primary}]`);
- `language_spoken`;
- `functional_record_id` (the Farmer ID).

The gap is only in how these are served.

## What to build

Two read routes, exactly as in the OpenAPI file:

1. **`GET /api/v1/farmers?level=&code=`** returns the farmers of one area, page by page.
   - `level` is region, zone, woreda or kebele; `code` is the Master Data `level_value_id`.
     The match is on the farmer's **code hierarchy**, never on names, so a woreda code
     returns every kebele inside it.
   - `record_status` defaults to `ACTIVE`.
   - **Cursor paging** (keyset on an indexed column, not OFFSET). `page_size` is up to 1,000.
     The cursor stays stable while records change, so a region of several hundred thousand
     farmers can be read to the end.
   - `updated_since` filters on the last approval time, phone changes included.
2. **`GET /api/v1/farmers/{farmer_id}`** returns one farmer by Farmer ID, in the same row
   shape. It returns 404 when the ID is unknown.

**Row shape** (both routes):
- `farmer_id`;
- `first_name`, `middle_name`, `last_name`;
- `language_spoken`;
- `primary_phone` (E.164) and `phones[{number, is_primary}]`;
- `region_code`, `zone_code`, `woreda_code`, `kebele_code`, plus the four names;
- `record_status`;
- `approved_at`.

Nothing else: no Fayda number, no birth date, no land data.

## Data rules

| # | Rule |
|---|---|
| B-1 | Area codes are Master Data `level_value_id`s. **Settle one format on every environment.** Today the ETH-OAN pack and DA intake use bare P-codes (`ET04080101`). The Dev catalogue migrated on 27 Oct uses unit ids (`kebele-ET04…`, per `oan-helm` commons-services values), and DA's `DA Administrative Area` examples use `ET04-W01-K03`. The DA backend keys `DA Region/Zone/Woreda/Kebele` on bare P-codes |
| B-2 | `primary_phone` is the phone marked primary; if none is marked, the first. Numbers are E.164. A farmer with no phone has `primary_phone: null` and `phones: []` (the DA side counts them; nothing is sent) |
| B-3 | `language_spoken` uses the registry's language code list as it is |
| B-4 | Errors are `application/problem+json` with value-free `code`s; submitted values are never echoed |
| B-5 | The routes never return farmers outside the requested area. The filter is enforced server-side (unlike the Dev behaviour of 29 Oct) |

## Access rule (decision for the registry / DPI owner)

There is no written rule today for reading farmer phones in bulk to send a broadcast.
Proposed:
- a Keycloak **client-credentials** client in the staff realm for each service (`da-services`,
  `bot-services`) with a new permission `farmer:contact:read`;
- **no per-farmer consent** for these two routes. A farmer agrees to receive official notices
  when registering, and the Telegram bot's privacy notice says so;
- every call is logged with the client id and the area or Farmer ID, never the phones.

If the owner decides that consent is required, the broadcast design has to change. Tell the
messaging team before building.

## How the DA backend calls it

The `da_broadcast_channels` add-on (`clients/beneficiary.py`):
1. calls `GET /api/v1/farmers` with `record_status=ACTIVE` and `page_size` from its settings;
2. follows `next_cursor` to the end;
3. commits each page's SMS together with the cursor, so it resumes where it stopped.

Until the routes are live, the kit's `stubs/stub_server.py` serves them from a fixture.

## Acceptance

- A woreda code returns exactly that woreda's active farmers, across pages, with no
  duplicates or gaps, while records are being edited.
- A region of 500,000 farmers reads to the end at `page_size=1000` without a timeout.
- A farmer's second phone marked primary becomes `primary_phone`.
- An unknown Farmer ID is a 404.
- A token without `farmer:contact:read` is a 403.
- A code in the wrong format is a 400 with `errors[{field: code}]`.
