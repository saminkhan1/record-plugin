# Record for Wexpro Agent

Record is Wexpro's record-review-run workflow extension for Apple-silicon
macOS. Wexpro Agent is based on the open-source
[Hermes Agent](https://github.com/NousResearch/hermes-agent), and intentionally
keeps Hermes' profiles, tools, skills, MCP integrations, scheduler, sessions,
and plugin contracts. Record captures one demonstrated Mac + Chrome workflow,
drafts it with the selected profile's model through a host-enforced read-only
discovery and typed-submit tool boundary, keeps the executable proposal
reviewable, and installs the approved result as a native Hermes Cron job or
signed webhook subscription.

Record never creates, names, or owns agent profiles. A normal installation
uses the existing default Hermes-compatible home. If the user has already
selected a named profile in Wexpro Agent, Record can follow that selection, but it
will fail rather than create a missing profile.

Desktop is the only Workflows launch surface. The dashboard directory exists
only because Hermes mounts the profile-scoped backend routes declared there;
it does not ship a second Workflows page.

## Product boundary

Record owns only the parts that are specific to recording and reviewing a
demonstrated workflow:

- the signed Mac recorder client and service;
- the Chrome MV3 capture extension and native-messaging bridge;
- recording evidence, drafting prompts, compilation, and review receipts;
- the Desktop page, sidebar item, status item, and command-palette actions.

The Wexpro host owns profiles, model/provider credentials, the full agent loop,
skills, tools, MCP servers, sessions, approvals, Cron persistence and
execution, webhook ingress, and the bundled Langfuse integration. Wexpro adds
two narrow host contracts required by Record: a plugin-facing full-agent run
facade and an atomic webhook-subscription mutation API. Record reuses those
contracts; it does not ship another runtime, agent loop, scheduler, execution
ledger, or observability client. Short clarification turns may use the host's
`ctx.llm`, but executable workflow drafting does not.

The launch flow is:

1. record Mac + Chrome activity;
2. stop and seal the recording;
3. draft through the selected profile's host agent, limited to skill/tool
   discovery, description, and the typed draft submission tool;
4. review or redraft the exact workflow;
5. set it as a standard Hermes Cron job that runs on a schedule or waits for a
   signed webhook event;
6. inspect execution in the normal Hermes Cron/session surfaces and in the
   Langfuse project configured for that selected profile.

Workflows is a one-shot creation transaction. Opening it starts clean unless a
recording is still actively capturing on the Mac. Stopped recordings and draft
artifacts never replace the new-workflow form; after installation, Cron owns
ongoing workflow management.

The port preserves the recorder's capture and secure-input behavior. Recorded
URLs retain ordinary query and fragment data needed for replay while values of
credential-like query parameters are redacted.

The final draft crosses one typed boundary, `record_submit_draft`, and is then
validated and sealed before publication. This boundary follows upstream
Hermes' plugin, Desktop, Cron, session, and tool contracts plus the two narrow
Wexpro host extensions above.

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

Existing named profiles remain entirely owned by the Wexpro/Hermes host. If Hermes
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
- a compatible Wexpro Agent/Desktop build with the upstream plugin APIs plus
  `PluginContext.agent.run` and atomic `mutate_subscriptions` support;
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

These host requirements are checked as concrete features rather than an exact
fork version number. Both plugin registration and the installer fail closed
when the required callables are absent. Record never vendors or patches the
agent runtime during installation.

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
export HERMES_RELEASE_BUILD=1
export APPLE_TEAM_ID='TEAMID'
export HERMES_RECORDER_SIGNING_IDENTITY='Developer ID Application: Example (TEAMID)'
npm run package:mac
```

The published Chrome Provider 0.2.3 archive was produced with Node 26.5.0
(bundled zlib 1.2.12). Use that runtime for Record release packaging; the
release preflight rejects any runtime whose compressed ZIP bytes do not match
the approved Store archive SHA-256 instead of changing the recorded hash.

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
pass. It promotes `dist/record-<version>-macos-arm64.zip` together with
`dist/record-<version>-macos-arm64.release.json`, which binds the exact archive
checksum and size to its source commit, payload manifest, Team ID, and both
accepted Apple submissions. Publish both files. The command fails without
stored credentials; the code-signing key alone is not notarization
authentication.

## Install

After notarization and Chrome Web Store publication, extract the ZIP and
double-click `Record Installer.app`. From this checkout, the equivalent
command is:

```bash
open "dist/macos-arm64/Record Installer.app"
```

Without an explicit profile argument, the backend target follows the retained Hermes
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

The Web Store item is published at extension version `0.2.3`. Its reviewed
release ZIP and clean source commit are bound in `store-identity.json`; the Mac
packager reproduces those exact extension bytes before allowing `--chrome
store`. `npm run package:chrome:release` requires a clean Git HEAD and binds
that commit to the deterministic ZIP receipt. Use `--chrome none` only for
local lifecycle validation on a machine whose reviewed native-host
registration is already present; it is not customer-distribution proof.

Restart the backend for the work profile just installed so its Python plugin
routes reload. Hermes Desktop watches its primary profile's
`desktop-plugins/` directory and hot-reloads the single ESM surface.

## Chrome provider

The packaged extension source is under the selected profile home:

```text
$PROFILE_HOME/plugins/record/chrome_capture/extension
```

The install path registers the manifest's fixed Web Store identity and allows
only that extension origin in Chrome's native-messaging manifest. Customer
packaging additionally proves that the embedded extension files reproduce the
approved Store archive and have not changed since its recorded source commit.
Restart Chrome after first registration. During local extension development,
the same directory can be loaded with Chrome's **Load unpacked** flow; its
manifest key preserves the same extension ID and therefore the same native-
host allow-list.

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

Drafting uses the Wexpro/Hermes agent for the profile currently selected in
Desktop, including its model, provider, and credentials. The host limits that
turn to `skills_list`, `skill_view`, `tool_search`, `tool_describe`, and the
typed `record_submit_draft` boundary. It does not expose arbitrary tool
dispatch or side-effecting profile tools while customer recording evidence is
in the prompt. A profile is the agent identity; Record
does not add a second agent identifier. Users can select any profile where
Record is installed and enabled. The profile carried in a Record request is
only a stale-view guard: the backend accepts its own active profile and
rejects a mismatched hint instead of crossing profile boundaries.

Job installation dispatches the host's upstream-compatible `cronjob` tool. The resulting job appears
in Hermes' normal Cron view, and executions use normal Hermes sessions, state,
tool dispatch, and approval behavior. Record keeps only the review and install
receipts that bind the workflow the user chose to the Hermes-owned job ID.
The human reviews the complete draft before Record creates or updates that
job. Runtime approval state is not stored in a Record draft: interactive
dangerous-command decisions and headless Cron policy remain profile settings.
Stock Cron cannot pause for a person; `cron_mode: deny` blocks a protected
command and `cron_mode: approve` permits it unattended.

Configure the bundled Langfuse plugin in every profile that should be traced.
Profiles may share one Langfuse project or use separate projects. Host-agent
drafting and any host-LLM clarification turns use Record task/session IDs and
the host's ordinary lifecycle hooks, so the bundled Langfuse plugin remains
the sole trace client and store.

Desktop supports time-backed jobs in any installed local profile and
event-backed jobs in the Default profile. Record does not add a
second receiver: it installs a reviewed dynamic subscription into Hermes'
profile-scoped, HMAC-authenticated webhook surface.
Matching deliveries use Hermes' native event filtering, replay protection,
payload templating, skill loading, agent sessions, and delivery behavior.
Cron continues to own only time-backed workflows. Because the current gateway
receiver is rooted in the default home, Record refuses webhook publication in
named profiles. In Default, it proves both receiver health and that the running
gateway loaded the exact HMAC-protected route before reporting installation.

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
