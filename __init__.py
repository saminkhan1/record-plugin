"""Record standalone plugin."""


def register(ctx):
    """Bind Hermes-owned plugin services used by the Record backend."""

    from .record.runtime import set_plugin_context

    set_plugin_context(ctx)
