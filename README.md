# Hermes Crunch

Hermes Crunch is a standalone Hermes plugin for recording a real macOS/Chrome
workflow, turning the evidence into a reviewable execution, and installing the
approved result as a native scheduled or authenticated event-triggered Cron
job.

This repository intentionally contains only plugin-owned code. It is developed
against an upstream-aligned Hermes core and a separate native/release overlay:

```text
hermes-agent-upstream-main/      upstream-compatible Hermes host
hermes-crunch-plugin/            this repository
hermes-crunch-distribution/      native recorder, Chrome packaging, release QA
```

## Boundary

The dashboard manifest adds `/workflows` and its profile-aware FastAPI routes.
The Python plugin registers:

- four private, service-gated drafting tools for bounded evidence reads, skill
  discovery, and non-executing requirement checks;
- the generic `hermes-crunch` Cron authorization provider; and
- one `pre_tool_call` hook that matches reviewed operations and obtains a
  fresh human decision on the exact pinned route.

The drafting agent receives only the private drafting toolset plus vision. It
cannot execute terminal commands, control browsers, modify Cron, or write
files. Ordinary Hermes conversations do not receive these tool schemas.

Hermes owns plugin discovery, profiles, models, skills, tool dispatch, gateway
adapters, Cron persistence/execution, the generic provider envelope, and the
per-run authorization scope. Crunch owns its evidence model, compiler, policy
matcher, explicit approval route, fresh-decision UX, and durable proposal,
revision, run, and operation receipts.

A protected job fails closed before its action when the plugin is absent, its
policy is invalid, the reviewed gateway route is disconnected, no capable
adapter is live, or the run identity is missing. Cron configuration and
reusable session/permanent approvals cannot auto-approve a Crunch boundary.
Every no-agent or hybrid script is shown as exact source and bound to its
script and execution SHA-256 digests before Hermes starts the subprocess.
When its review boundary is enabled, Hermes asks once before execution. An
operator can disable that prompt during final review, but the immutable digest
binding remains because arbitrary Python or shell cannot be soundly proven
read-only.

## Recording

One append-only bundle is written under:

```text
<HERMES_HOME>/crunch/recordings/<workflow-id>/<session-id>/
```

Native input, Accessibility context, active-application video, Chrome DOM
intent, screenshots, downloads, and provider transitions are normalized into
one trace. Raw input is the only source of native actions; passive
Accessibility observations never fabricate clicks or typing. Password and
secure-input values remain redacted.

Chrome capture lives in `chrome_capture/`. Its MV3 extension, native-messaging
host, provider arbitration, live handshake, bounded screenshots, and
append-only lifecycle are plugin-owned.

The signed Swift recorder is deliberately outside Hermes core at:

```text
../hermes-crunch-distribution/native/recorder-service
```

Production and the standalone runner set `HERMES_RECORDER_CLIENT` to the staged
signed helper. Development may set `HERMES_CRUNCH_NATIVE_ROOT` to an overlay
containing either:

```text
hermes-recorder/hermes-recorder
recorder-service/.build/<configuration>/hermes-recorder
```

No host-monorepo native path is consulted.

## Review, compilation, and installation

The evidence pack is bounded before drafting. The drafter chooses the smallest
reliable native execution shape—deterministic script, hybrid, or agent—and
keeps material ambiguity reviewable. The compiler then:

1. validates execution, trigger, requirements, and authorization identities;
2. proves every protected agent operation or exact script maps to a real
   runtime boundary;
3. stages scripts and hashes the exact artifact set;
4. produces an approval envelope bound to payload, provenance, and artifacts;
5. reserves a disabled Cron job through the generic install contract;
6. installs an authenticated webhook route when selected; and
7. activates the reservation only after the other phases succeed.

Triggered routes enqueue authenticated payloads directly into the durable Cron
trigger queue. They never start a separate webhook agent plan. Missing provider
or route state is retryable and does not consume the queued event.

## Build and test

From this repository:

```bash
npm run build:dashboard
npm run test:dashboard
npm --prefix chrome_capture/extension test
```

Run Python tests with the aligned host's isolation wrapper:

```bash
../hermes-agent-upstream-main/scripts/run_tests.sh "$PWD/tests"
```

The wrapper isolates credentials and `HERMES_HOME`; do not run the suite with
direct `pytest`.

Native builds, Chrome Store packaging, Tart acceptance, signing, notarization,
and customer installation are owned by the sibling distribution repository:

```bash
cd ../hermes-crunch-distribution
npm test
```

See
[`../hermes-crunch-distribution/README.md`](../hermes-crunch-distribution/README.md)
for the current overlay runner, native build, release qualification, and Chrome
registration commands.

## Runtime requirements and limits

- macOS is the implemented native recorder platform.
- Google Chrome and Chrome Canary are the specialized browser providers.
- Chrome-internal pages that reject content scripts cannot provide a live DOM
  segment.
- Accessibility, Screen Recording, and Input Monitoring must be granted to the
  signed recorder identity.
- The Chrome extension/native-host handshake must be live before a recording
  is accepted.
- A protected scheduled run requires the exact reviewed home route to be live
  on an adapter that supports fresh approval controls, and therefore currently
  requires Hermes' built-in in-process Cron scheduler. External scheduler
  callbacks do not carry those live gateway adapters.
- Event triggers require an enabled authenticated webhook receiver and one of
  the signature schemes accepted by Hermes ingress.
