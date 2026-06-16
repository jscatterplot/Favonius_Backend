# VDV 463 JSON Schemas

The VDV 463 message-validation schemas are **not redistributed in this repository**.

They are authored and copyrighted by the **VDV (Verband Deutscher Verkehrsunternehmen)**
and published at <https://github.com/VDVde/VDV463> (`schema/` directory). That upstream
repository carries no open-source license, so its files are not vendored here.

## How schemas are obtained at runtime

`src/adapters/vdv463/messages.py::SchemaRegistry` resolves schemas in this order:

1. **Cache dir** — `VDV463_SCHEMA_DIR` if set, else `.cache/vdv463/schemas/` (gitignored).
2. **Fetch** — downloads the official schemas from
   `https://raw.githubusercontent.com/VDVde/VDV463/main/schema` into the cache on first use.
3. **Minimal inline fallback** — a small set of in-house schemas (defined in `messages.py`)
   sufficient for structural validation and the test suite when no network/cache is available.

## Using your own copy (offline / air-gapped)

Place the seven schema files (`MessageStructure.json`, `BootNotificationRequest.json`,
`BootNotificationResponse.json`, `ProvideChargingRequestsRequest.json`,
`ProvideChargingRequestsResponse.json`, `ProvideChargingInformationRequest.json`,
`ProvideChargingInformationResponse.json`) in a directory and point `VDV463_SCHEMA_DIR` at it.
Obtain them directly from the VDV repository above, subject to VDV's terms.
