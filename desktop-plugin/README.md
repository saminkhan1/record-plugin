# Record Desktop plugin

This is the native Hermes Desktop surface for Record. It is a single
plain ESM plugin and uses only the supported `@hermes/plugin-sdk`, React, and
the profile-scoped Record `plugin_api.py` namespace.

The page reads `host.state.profile`; Hermes Desktop owns profile selection,
and Record neither pins a profile nor adds another selector.

It keeps the existing product lifecycle:

1. provision Mac + Chrome capture and record one demonstration;
2. stop and draft from the sealed recording;
3. review or revise the draft with Hermes;
4. set the reviewed workflow as a standard Hermes Cron job, either on a
   schedule or awaiting a signed webhook event.

Desktop is the only Workflows launch surface. The dashboard manifest is an
API-only host adapter and has no customer workflow page. The Desktop plugin
does not duplicate Hermes execution, scheduling, sessions, approvals, or
observability, and does not add a redaction layer.

The page starts with a new workflow unless the native recorder is actively
capturing. It retains recording, drafting, and review state only inside that
creation transaction; stopped recordings and old drafts are not reopened.

The plugin ships one canonical English message bundle through
`ctx.i18n.register`. `usePluginI18n` follows the Desktop app's active locale and
uses Hermes' standard English fallback for locales Record does not maintain.

## Package

```sh
cd desktop-plugin
npm run check
```

This command is release validation, not a runtime compile step. It validates the
disk-plugin import fence and copies the byte-identical source to
`dist/record/plugin.js`.

Install that file at:

```text
$DESKTOP_PRIMARY_HERMES_HOME/desktop-plugins/record/plugin.js
```

This is the default Hermes root unless Hermes Desktop has a named primary
profile in its own `active-profile.json`. It is one UI copy for the mounted
Desktop window; `host.state.profile` and `ctx.rest` follow live sidebar profile
switches without duplicating the UI into every work profile.

The regular Python plugin must be installed and enabled in each work profile
that should use Record so its `dashboard/plugin_api.py` routes mount at
`/api/plugins/record/`. Hermes Desktop watches `desktop-plugins/` and
hot-reloads the file.

## Backend adapter contract

All non-Hermes state stays behind the plugin namespace. The ESM centralizes
these Desktop-only adapter calls:

- `POST /recordings/capture-authorization`
- `POST /recordings/start-desktop`
- `POST /recordings/{recording_key}/stop-desktop`
- `GET /recordings/active`
- `GET /webhook-status`
- `POST /recordings/set-job`

`set-job` validates the reviewed digest and delegates job persistence and
execution to Hermes Cron. For webhook mode it also requires the active
profile's Hermes receiver to be ready, installs one signed dynamic subscription,
and returns its URL and one-time signing secret. The receipt stores only
non-secret route metadata.
