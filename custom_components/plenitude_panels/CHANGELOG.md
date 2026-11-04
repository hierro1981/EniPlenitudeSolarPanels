# Changelog

## 0.10.2

### Fixed

- Declare the HTTP dependency and load after Recorder when available, preserving operation without Recorder while fixing Home Assistant validation and startup ordering.
- Align the Babel requirement with current Home Assistant integrations to avoid package conflicts; older Home Assistant installations using a different pinned Babel version may require an update.
- Add the solar energy category for Home Assistant's integration catalog.

## 0.10.1

### Fixed

- Move the integration to the repository-root `custom_components/plenitude_panels/` directory for HACS installation; the installed domain and existing configuration remain unchanged.
- Complete documentation, issue tracker and codeowner metadata; declare the integration as a cloud-polling hub.
- Add HACS metadata for Italy, bundled brand assets and HACS/Hassfest validation workflows. Default-catalog inclusion still requires successful validation, a GitHub release and acceptance by HACS.
- Replace the repository README with Italian installation, browser-session configuration and Energy Dashboard guidance; preserve detailed technical documentation and isolate optional Node.js tools for manual removal.

## 0.10.0

### Added

- Configure the browser User-Agent during setup, Settings and reauthentication. Existing entries default to Chrome 156 on Windows without requiring recreation. Copy Cookie and User-Agent from the same successful browser request.

### Fixed

- Replace hardcoded Chrome 155 client hints with version, platform and mobile hints derived from the configured Chromium User-Agent; omit Chromium hints for other browsers.
- Report HTTP 406 as a rejected request rather than confirmed cookie expiry, with actionable configuration errors and safe endpoint/status/content-type/cookie-name logging. The server may still reject a client for other session or protection checks.
- Retain submitted connection fields after validation errors and handle request/validation timeouts as connection failures instead of unhandled exceptions.

## 0.9.0

### Added

- Add Configure > Repair statistics: analyze a completed day against the aggregate API, select affected panels, review signed daily totals and falling cumulative sums, and explicitly confirm replacement of normalized hourly/five-minute sums. Existing valid nonselected panels are preserved; selected panels share the aggregate remainder by nominal power (equal shares as fallback), retaining previously recovered energy as a minimum.
- Save the latest repair's previous values and transactional audit state before writing. Configure > Undo last statistics repair restores them, including later appended statistical offsets; concurrent changes to saved rows prevent overwriting them. A durable journal reconciles interrupted commits across restarts.
- Coordinate repairs with recorded, compiled pending recoveries for that date, removing their later artificial increases and acknowledging their audits without increasing live totals. Requests that are not yet compiled, or belong to other pending dates, must be resolved first. No raw sensor history is rewritten.

### Fixed

- Clamp finite negative API production/power samples to zero while retaining missing/nonfinite values as unavailable; expose negative aggregate sample counts in the repair analysis.
- Restore cumulative sensors in Wh when Home Assistant previously stored their displayed state in kWh or MWh, before subtracting recovered Wh.
- Fill historical gaps from the nearest available following reference instead of a distant current fallback when no earlier baseline exists. Validate monotonic normalized sums before and after automatic correction, rolling back falling sequences and directing users to the repair tool.
- Repair daily curves and their joins while preserving later production increments. Align short-term cumulative coordinates with hourly sums; recreate missing short-term buckets only within Recorder retention. Whole-hour-offset timezones, including 23/25-hour DST days, are supported; ambiguous API timestamps remain deferred.

## 0.8.4

### Fixed

- Preselect every panel without available production data, as well as panels with a recorded zero, in the manual recovery recipient form. Panels with positive totals or partial positive readings are not suggested.
- Suggest an editable original-production value of 0 Wh when all panels have no data, without changing the unavailable markers or treating the suggestion as a measured total. Partial or uncertain data does not trigger the zero suggestion.
- Preserve recipient selections and energy values entered by the user when editing the preview or correcting validation errors. Update English and Italian guidance to explain these defaults.

## 0.8.3

### Added

- After manual recovery, keep the completion screen open with dated Previous day and Next day shortcuts and a Close action. Each shortcut starts a fresh analysis and confirmation, without reusing the completed recovery's recipients or amounts.
- Hide Next day when it would select today, and reject today/future dates before fetching data. Keep a failed requested date selected for retry. Clearing the completed preview prevents it from being applied again while navigating.

## 0.8.2

### Fixed

- Display the recovery legend as a bulleted list, with separate lines for unavailable data (—), observed lower bounds (≥) and estimated allocations (≈), in both analysis and confirmation screens and in English and Italian.

## 0.8.1

### Fixed

- Read daily recorded energy with Home Assistant's Energy Dashboard statistics/change API, in Wh, rather than requiring every hourly point and an exact midnight baseline. Preserve each panel's available data independently, including when another panel has no history.
- Distinguish verified recorded totals, partial observed lower bounds and unavailable values. Leave an unknown manual total explicitly blank instead of suggesting zero, and suggest recipients with a verified zero recorded total.
- Subtract only recoveries already included in the requested day's statistics. Pending corrections no longer suppress valid totals from every panel; a possible still-unmoved recovery spike affects only the panel concerned.
- Present recovery summaries one item per line and show a per-panel table with original detected production, previous recovery and the calculated new allocation. Retain entered totals and selected recipients when editing a preview, and clear outdated previews after a new analysis or invalid input.
- Format summary numbers using the requesting user's Home Assistant number-format preference, falling back to the user's language, browser request language and configured language. Support decimal/comma, comma/decimal, space/comma, Swiss grouping and no-grouping modes; browser-system mode uses request language preferences. Add the Babel locale-data dependency, installed automatically by Home Assistant. Formatting affects labels only, not calculation precision or native numeric inputs.

## 0.8.0

### Added

- The Configure button now opens a menu with Settings and Recover production, with a guided date analysis, optional manual recipient/energy input, and explicit confirmation preview. Manual recovery works independently of the automatic recovery switch and can request older dates available from the aggregate API.
- Supplement local sampling history with original Recorder cumulative-state deltas, using recorded sampling IDs without filling gaps. Suggest original daily energy from complete hourly statistics when available and no statistical corrections are pending.
- When history is insufficient, request recipient panels and the original energy already counted across all panels (excluding previous recoveries). Split only the remaining deficit by nominal power, using equal shares as fallback and labeling its aggregate-derived temporal profile as an estimate.
- Persist a per-day allocation ledger; repeated requests apply only additional energy. Reject stale previews and serialize manual operations with polling. Existing recoveries are retained rather than reduced or reassigned.
- Reconstruct missing historical statistical buckets and baseline sums for the requested day, retaining short-term buckets only within Recorder's retention period. Handle increases already present in Recorder's initial baseline without creating a negative recovery-day spike.
- Migrate pending 0.7 recoveries and reconstruct completed per-day amounts from Recorder audit markers when available; defer manual recovery when a legacy amount cannot be attributed safely.

### Fixed

- Finish pending statistical corrections on the five-minute session heartbeat even when live panel polling is configured less frequently. Statistical audit markers now support multiple incremental recoveries for the same panel and date.
- Restore in-memory recovery state if saving an allocation fails, so uncommitted energy is not published.

## 0.7.0

### Added

- Optional POD configuration and a missing-production recovery switch, enabled by default. Recovery waits for a configured POD; disabling it preserves live monitoring and previously recovered energy.
- Persist seven days of panel samples and reconcile completed days hourly against the aggregate quarter-hour production API, retrying delayed or incomplete data.
- Allocate positive residual energy only to panels without positive daily readings, proportionally to nominal power (equal shares if any required power is unavailable), supporting any number of panels.
- Restore recovered energy across restarts and correct recorded Energy Dashboard sums in the original intervals, with transactional audit markers preventing duplicate historical corrections. Already allocated corrections finish even when new recovery is disabled.
- Expose recovered Wh, last recovered day and recovery status on cumulative production sensors. Missing live coverage, changed panel inventories and ambiguous daylight-saving timestamps defer recovery rather than guessing.
- Assign live readings to the preceding five-minute interval for reconciliation, including the previous day at midnight. Isolate recovery storage by account/supply, and restart sample history when a previously configured POD changes.

### Fixed

- Reject negative and nonfinite live energy values and accept Italian decimal formatting before accumulating energy.

## 0.6.2

### Changed

- Update the User-Agent and browser client hints to Chrome 155 on Windows, matching the latest browser capture.

## 0.6.1

### Changed

- Identify portal API requests as Google Chrome on Windows, including browser client hints and fetch metadata headers.

## 0.6.0

### Added

- A 5-to-60 minute slider, with the selected minute value shown, for configuring the panel data refresh interval.

## 0.5.2

### Fixed

- Renew the browser session proactively every five minutes, retaining this maintenance cadence when panel data is configured to refresh less often.
- Parse Plenitude's nonstandard `Set-Cookie` expiry syntax and persist successful cookie rotations even when a later panel request fails.
- Add debug logging for successful API responses that reports cookie update metadata without exposing cookie values.

### Changed

- Send the portal home-page `Referer` with panel API requests, matching the browser flow.

## 0.5.1

### Fixed

- Persist all cookie updates returned by successful Plenitude API responses, including `GYSESSION` rotations and cookie removals, rather than handling only `GAESA` from `c360/init`.

## 0.5.0

### Changed

- Replaced the periodic Gigya token refresh with `c360/init` GAESA rotation.
- Removed Gigya credentials from the integration configuration.

### Fixed

- Preserved configuration edits while a previous coordinator is finishing a session update.

## 0.4.0

### Added

- `Average Power (5 min)` sensor in watts, using the API's unconverted five-minute average.

## 0.3.0

### Added

- A Configure screen for replacing browser credentials and changing integration settings.

### Fixed

- Converted the API's hourly-equivalent production value to energy for its five-minute window before reporting or accumulating it.

## 0.2.0

### Added

- Cumulative `Total Production` energy sensor for every panel.
- Home Assistant state restoration for cumulative production after restart.
- Five-minute sample de-duplication to prevent repeated refreshes from being counted twice.
- Energy Dashboard setup instructions.

### Changed

- `Production (5 min)` remains an interval display sensor and no longer declares a measurement state class.

## 0.1.0

### Added

- Initial Home Assistant integration for Plenitude photovoltaic panels.
- A device for every panel returned by the Plenitude API.
- Production sensor reporting energy produced during the previous five minutes.
- Irradiance sensor when the API provides the value.
- Panel metadata from the `details` endpoint as entity attributes.
- Automatic Gigya session renewal with persisted `GYSESSION` and `GAESA` cookies.
- Reauthentication flow for expired browser credentials.
