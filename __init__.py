"""Hermes Crunch standalone plugin."""


def register(ctx):
    """Register the private drafting surface and Cron authorization provider."""

    from hermes_crunch.authorization import register_authorization
    from hermes_crunch.drafting_tools import register_drafting_tools

    register_drafting_tools(ctx)
    register_authorization(ctx)
