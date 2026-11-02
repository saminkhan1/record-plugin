# Record

> **Demonstrate Mac + Chrome workflows, review drafted steps, and install automated Hermes Agent jobs.**

Record is a standalone extension for stock Hermes Agent. On Apple-silicon macOS it records demonstrated Mac + Chrome workflows, drafts repeatable automation steps using the Hermes profile currently selected in Desktop, keeps every draft transparently reviewable, and installs the reviewed result directly as a standard Hermes Cron job or dynamic webhook trigger.

---

## Key Components

- **🖥️ Hermes Recorder Service**: Native Apple-silicon daemon signed with Developer ID; records screen and user events with secure-input privacy protection.
- **🌐 Record Chrome Provider**: Manifest V3 Chrome extension and native-messaging bridge capturing DOM interactions, navigations, and downloads during active recordings ([Documentation](chrome_capture/extension/README.md)).
- **📱 Desktop Plugin**: Native Hermes Desktop ESM plugin providing the Workflows review and approval surface.
- **⚙️ Drafting & Compiler Engine**: Translates demonstrated multimodal traces into typed Hermes task actions via `ctx.llm`.

---

## Workflow Lifecycle

```text
┌───────────────────────┐
│ 1. Demonstrate        │  Mac Screen + Chrome Provider capture actions & DOM evidence
└──────────┬────────────┘
           ▼
┌───────────────────────┐
│ 2. Seal & Ingest      │  Cryptographic recording seal; redact credentials & passwords
└──────────┬────────────┘
           ▼
┌───────────────────────┐
│ 3. Draft              │  Synthesize structured actions via selected profile's ctx.llm
└──────────┬────────────┘
           ▼
┌───────────────────────┐
│ 4. Review & Verify    │  Human inspects exact commands, parameters, and capabilities
└──────────┬────────────┘
           ▼
┌───────────────────────┐
│ 5. Install & Execute  │  Stock Hermes Cron schedule or signed webhook dynamic ingress
└───────────────────────┘
```

---

## Quick Navigation

- [Product Boundary](#product-boundary)
- [Installed Layout](#installed-layout)
- [Record Chrome Provider](#chrome-provider)
- [Requirements](#requirements)
- [Quick Start & Local Development](#quick-start)
- [Build the Mac Package](#build-the-mac-package)
- [Installation Guide](#install)
- [Profiles, Cron, Approvals, & Langfuse](#profiles-cron-approvals-and-langfuse)
- [Runtime Data](#runtime-data)
- [Testing & Validation](#implementation-validation)

---

## Product boundary

Record owns only the parts that are specific to recording and reviewing a
demonstrated workflow:

- the signed Mac recorder client and service;
- the Chrome MV3 capture extension and native-messaging bridge;
- recording evidence, drafting prompts, compilation, and review receipts;
- the Desktop page, sidebar item, status item, and command-palette actions.

Stock Hermes owns profiles, model/provider credentials, `ctx.llm`, skills,
tools, sessions, approvals, Cron persistence and execution, webhook ingress,
and its bundled Langfuse integration. Record does not ship a Hermes runtime,
fork the agent loop, maintain a second scheduler, add an execution ledger, or
create a second observability client.

The launch flow is:

1. record Mac + Chrome activity;
2. stop and seal the recording;
3. draft through the selected profile's `ctx.llm`;
4. review or redraft the exact workflow;
5. set it as a standard Hermes Cron job that runs on a schedule or waits for a
   signed webhook event;
6. inspect execution in the normal Hermes Cron/session surfaces and in the
   Langfuse project configured for that selected profile.

Workflows is a one-shot creation transaction. Opening it starts clean unless a
recording is still actively capturing on the Mac. Stopped recordings and draft
artifacts never replace the new-workflow form; after installation, Cron owns
ongoing workflow management.

The port preserves the recorder's current capture and secure-input behavior.
The packaging and Desktop layers add no new filtering or redaction feature.

This boundary follows the host application's documented plugin system, Desktop
app, Desktop Plugin SDK, and Cron contracts.

## Installed layout

The normal Mac package installs into Hermes' existing default home. Record
does not create a product-specific profile. It installs one Python plugin, one
Desktop UI copy, one signed recorder, and one Chrome launcher:

```text
$HERMES_ROOT/
├── record/native-recorder/                 stable signed Mac identity
│   ├── hermes-recorder
│   ├── Hermes Recorder Service.app
│   ├── recorder-manifest.json
│   └── installation.json
├── record/chrome-provider/                 machine-wide native-host launcher
├── desktop-plugins/record/                  Desktop UI
│   └── plugin.js                           plain ESM Desktop plugin
└── plugins/record/                          Python/backend plugin
    ├── plugin.yaml
    ├── dashboard/plugin_api.py
    └── chrome_capture/
```

Existing named profiles remain entirely owned by stock Hermes. If Hermes
Desktop already has one selected, the installer may follow Hermes' resolver,
but it verifies that profile already exists and never creates or renames it.

The signed recorder stays at the root-global path. Its client receives the
Hermes-owned recording root on every invocation; sharing the executable does
not create agent state, a profile, or another runtime.

The Desktop file uses only `@hermes/plugin-sdk`, React, and
`react/jsx-runtime`, which are the imports exposed by Hermes Desktop. Backend
calls stay under `/api/plugins/record/` and therefore use the active
profile's Hermes backend and state.

Hermes Desktop remains the profile selector. When the user switches among
installed and enabled local profiles, `host.state.profile` changes and
`ctx.rest` routes to that profile's backend. Record does not select, pin, or
emulate a profile backend. The launch and support scope covers local profile
backends only. Remote-connected profiles have not been qualified, and the
Desktop Plugin SDK does not expose a trusted connection-mode signal that
Record could use to present them as supported.

Chrome registration is machine-wide. Its native-messaging manifest points to
a small launcher under the Hermes root at `~/.hermes/record/chrome-provider/`.
The launcher uses Hermes' existing Python only as an interpreter. It accepts
receipt-authorized release installs, plus the explicitly marked local
`release:false`/`chromeMode:local`/`signingIdentity:adhoc` receipt emitted by
`npm start`; in both cases the extension ID and version must match
the machine registration and refuses to select a host without an active
Desktop recording. The native host follows the one global
active-recording pointer, whose payload identifies the profile home and
recording directory that started capture. It is not bound to a fixed profile
backend and does not copy or bundle Hermes.

## Requirements

- Apple-silicon Mac (`arm64`) running macOS 13 or newer;
- a stock Hermes Agent/Desktop release with the Desktop Plugin SDK,
  plugin-owned `ctx.llm`, standard lifecycle hooks, and public plugin tool
  dispatch;
- Node.js 20 or newer and Apple's Swift toolchain to build locally;
- one stable macOS code-signing identity;
- Google Chrome and the published Record Chrome Provider Web Store extension
  for customer installation;
- Accessibility, Screen Recording, and Input Monitoring permission for the
  signed Hermes Recorder Service identity.

Permissions are tied to the recorder's signing identity. Rebuilding with a
different identity can require granting the macOS permissions again.
The installer registers one canonical app path, but never resets or edits
TCC. The user grants Accessibility, Screen Recording, and Input Monitoring to
that stable Developer ID app; a row for an older ad-hoc or profile-local build
is not release evidence.

These Hermes requirements are capability-gated rather than inferred from a
version number. The installer probes them and fails closed. Record never
vendors or patches a Hermes runtime during installation.

## Build the Mac package

For a clean local build, default-home install, and Hermes Desktop launch, use
one command from this repository:

```bash
npm start
```

That command removes generated Record and Desktop-plugin outputs, runs the
Python and Chrome tests, builds the dashboard, Desktop plugin, and signed Swift
recorder, installs Record into the existing default `~/.hermes` home, refreshes
the Chrome native-host registration, clean-builds Hermes Desktop, and launches
it. It does not create a Hermes profile or delete recordings. Local `npm start`
builds use an ad-hoc recorder signature when
`HERMES_RECORDER_SIGNING_IDENTITY` is unset, so macOS can require permission
approval after a clean rebuild. Set that variable to a usable stable identity
to preserve the recorder's privacy identity across builds. Release packaging
still requires a timestamped Developer ID signature and never uses the local
ad-hoc fallback. Use
`npm run start:build-only` for the identical clean build and installation
without launching Desktop.

The unavoidable manual steps remain granting macOS permissions, publishing or
updating the Chrome Web Store extension, and performing the actual recording.

Build the web and Desktop surfaces without native packaging:

```bash
npm run build
```

Build the complete release-gated arm64 package:

```bash
npm run check:versions
export HERMES_RELEASE_BUILD=1
export APPLE_TEAM_ID='TEAMID'
export HERMES_RECORDER_SIGNING_IDENTITY='Developer ID Application: Example (TEAMID)'
npm run package:mac
```

`check:versions` requires the package, backend plugin, dashboard, Desktop
plugin, recorder bundle, and recorder protocol to carry the same Record
version. Release mode additionally requires a clean checkout whose `HEAD` is
tagged exactly `v<version>`. The native and outer package manifests retain that
commit and tag, and packaging rechecks the source after copying every runtime
file. The Chrome Provider has its own Store version and remains independently
versioned.

If exactly one Developer ID Application identity is available, the build can
select it automatically. Otherwise it asks for the explicit identity. The
package command always requires release mode and the expected Apple Team ID;
it never emits an ad-hoc launch package whose macOS privacy identity would
change.

The package is written to:

```text
dist/macos-arm64/
└── Record Installer.app/
    └── Contents/
        ├── MacOS/record-installer
        └── Resources/payload/
            ├── manifest.json
            ├── native/hermes-recorder/
            ├── plugins/record/
            ├── desktop-plugins/record/plugin.js
            └── scripts/install-mac.py
```

The nested `manifest.json` inventories and SHA-256 hashes the complete payload.
The build verifies that the recorder client, service, and native installer
launcher are arm64-only and release-signed by the configured Apple Team ID,
then seals the payload inside the signed Installer app.

Release mode enables the hardened runtime and timestamped Developer ID
signing. A customer artifact must then use the same keychain-profile
notarization convention as Hermes Desktop:

```bash
export APPLE_NOTARY_PROFILE='stored-notarytool-profile'
npm run notarize:mac
```

Create that profile beforehand with `xcrun notarytool store-credentials`.
The command works on a temporary copy, separately notarizes and staples the
recorder service, updates only the service ticket covered by the payload
manifest, reseals and notarizes the outer Installer app, and promotes the
package only after codesign, stapler, Gatekeeper, and inventory validation all
pass. It writes `dist/record-<version>-macos-arm64.zip` and fails without
stored credentials; the code-signing key alone is not notarization
authentication.

## Install

After notarization and Chrome Web Store publication, extract the ZIP and
double-click `Record Installer.app`. From this checkout, the equivalent
command is:

```bash
open "dist/macos-arm64/Record Installer.app"
```

Without an explicit profile argument, the backend target follows stock Hermes
precedence: Hermes Desktop's persisted primary-profile preference, then
`~/.hermes/active_profile`, then the default profile. The Desktop ESM goes in
the one plugin root that the current upstream Desktop loader watches: the
persisted primary profile's root, or global `~/.hermes/desktop-plugins` when no
Desktop preference is set. A temporary sidebar profile selection is deliberately
not treated as a persisted installer target.

The install performs these implementation steps:

1. verifies the Installer app seal, Team ID, Gatekeeper result, payload hashes,
   recorder architecture, and nested signatures;
2. atomically installs or upgrades the root-global signed recorder;
3. atomically installs or upgrades the target profile's Python plugin and the
   one ESM copy in Hermes Desktop's primary plugin root;
4. registers only the canonical recorder app path with LaunchServices;
5. registers the published Chrome Web Store identity and native-messaging host;
6. runs `python -m hermes_cli.main --profile <target> plugins enable record
   --no-allow-tool-override`;
7. writes a mode-0600 profile receipt at
   `$HERMES_HOME/record/installation.json`.

Recognized Record installs upgrade transactionally; unrecognized files or
directories at any owned target fail closed instead of being overwritten.
The customer path needs neither Node nor a `hermes` executable on `PATH`; the
native launcher uses Hermes Desktop's managed Python runtime.

To target a named Hermes profile, pass its actual profile home. Hermes profiles
are isolated, so install and enable the plugin in each profile that should use
Record:

```bash
"dist/macos-arm64/Record Installer.app/Contents/MacOS/record-installer" \
  --hermes-home "$HOME/.hermes/profiles/my-agent"
```

Chrome registration is machine-wide. For additional profiles, keep the
existing registration and root-global recorder, and use `--chrome none`.
That mode refreshes the verified root-global bootstrap code but does not change
Chrome manifests or the Web Store registration. Recording still follows
whichever installed profile the user selects in Hermes Desktop:

```bash
"dist/macos-arm64/Record Installer.app/Contents/MacOS/record-installer" \
  --hermes-home "$HOME/.hermes/profiles/another-agent" \
  --chrome none
```

Use `--no-enable` only when the operator wants to enable the Python plugin
later through Hermes Settings or `hermes plugins enable`.

Removing Record from one work profile must not remove the primary Desktop UI,
`$HERMES_ROOT/record/native-recorder`, or the Chrome launcher while another
installed profile uses them. Whole-product cleanup may remove shared components
only after no Record-enabled profile, active recording, or recorder service
remains.

The repository currently records the Web Store identity as `assigned`, not
`published`. While that remains true, the installer deliberately rejects
`--chrome store`. Use `--chrome none` only for local lifecycle validation on a
machine whose reviewed native-host registration is already present; it is not
customer-distribution proof.

Restart the backend for the work profile just installed so its Python plugin
routes reload. Hermes Desktop watches its primary profile's
`desktop-plugins/` directory and hot-reloads the single ESM surface.

## Chrome provider

The **Record Chrome Provider** captures demonstrated user actions and DOM evidence in Google Chrome exclusively while a local macOS Record session is active. See the full [Record Chrome Provider Documentation](chrome_capture/extension/README.md) for architecture, security safeguards, and tests.

The packaged extension source is located under:

```text
chrome_capture/extension/
```

### Installation & Registration

The install path registers the manifest's fixed, assigned Web Store identity
and allows only that extension origin in Chrome's native-messaging manifest.
Restart Chrome after first registration. During local extension development,
the same directory can be loaded with Chrome's **Load unpacked** flow; its
manifest key preserves the same extension ID and therefore the same native-
host allow-list.

### Extension Development & Testing

```bash
# Run Chrome capture extension tests
npm run test:chrome

# Package extension into deterministic release ZIP for Chrome Web Store
npm run package:chrome
```

The global launcher discovers current installed plugin copies when Chrome
starts it. Removing the profile that originally performed Chrome registration
does not break another current profile installation. Selection only chooses
native-host code; the global active-recording pointer still determines the
recording directory, while Hermes Desktop's selected profile remains the
model, Cron, state, and Langfuse backend.

Chrome-internal pages and other pages that reject content scripts cannot emit
DOM evidence. That remains an evidence limitation and does not cause the
recorder to invent browser actions.

## Profiles, Cron, approvals, and Langfuse

Drafting uses the model, provider, keys, and skills of the profile currently
selected in Hermes Desktop. A profile is the Hermes agent identity; Record
does not add a second agent identifier. Users can select any profile where
Record is installed and enabled. The profile carried in a Record request is
only a stale-view guard: the backend accepts its own active profile and
rejects a mismatched hint instead of crossing profile boundaries.

Job installation dispatches the stock `cronjob` tool. The resulting job appears
in Hermes' normal Cron view, and executions use normal Hermes sessions, state,
tool dispatch, and approval behavior. Record keeps only the review and install
receipts that bind the workflow the user chose to the Hermes-owned job ID.
The human reviews the complete draft before Record creates or updates that
job. Runtime approval state is not stored in a Record draft: interactive
dangerous-command decisions and headless Cron policy remain profile settings.
Stock Cron cannot pause for a person; `cron_mode: deny` blocks a protected
command and `cron_mode: approve` permits it unattended.

Configure Hermes' bundled Langfuse plugin in every profile that should be
traced. Profiles may share one Langfuse project or use separate projects; each
successful Record `ctx.llm` call uses a profile-qualified Hermes session and
task ID so work remains attributable in a shared project without adding a
second agent identifier. Related draft, repair, and review traces share the
workflow/profile session. Record emits only Hermes' standard lifecycle hooks;
the bundled Langfuse plugin remains the sole trace client and store.

Desktop supports both time-backed and event-backed jobs. Record does not add a
second receiver: it installs a reviewed dynamic subscription into Hermes'
HMAC-authenticated webhook surface. For 0.1.2, webhook activation is limited to
the Default profile because Hermes' shared multiplex gateway does not yet
hot-reload isolated named-profile subscription registries. Scheduled workflows
remain available in every supported local profile.
Matching deliveries use Hermes' native event filtering, replay protection,
payload templating, skill loading, agent sessions, and delivery behavior.
Cron continues to own only time-backed workflows. Desktop reports named-profile
webhooks as unavailable and, in Default, refuses activation until the gateway's
webhook receiver is live.

## Runtime data

Recordings and plugin-owned review/install receipts live under the profile
currently selected in Desktop:

```text
$HERMES_HOME/record/
├── recordings/<workflow-id>/<session-id>/
├── install-receipts/
└── installation.json
```

Cron jobs, sessions, approvals, profile settings, and Langfuse configuration
remain in their normal Hermes-owned locations.

The machine-shared signed recorder and its build/install receipts live at
`$HERMES_ROOT/record/native-recorder/`. They contain executable provenance,
not workflow or agent state.

## Implementation validation

The main implementation checks are:

```bash
npm run build
HERMES_RELEASE_BUILD=1 APPLE_TEAM_ID=TEAMID npm run package:mac
"dist/macos-arm64/Record Installer.app/Contents/MacOS/record-installer" \
  --hermes-home /path/to/isolated/hermes-home \
  --chrome none
```

After installing into a real profile, validate the product lifecycle in
Hermes Desktop: record Mac + Chrome activity, stop, draft, review, set the
Hermes job, run it through the normal Cron surface, and inspect the resulting
Hermes session and profile Langfuse trace.
