try:
    import e2b  # noqa: F401
except ImportError:
    pass
else:
    from providers.e2b.provider import E2BProvider
    from providers.registry import register_vm_provider

    register_vm_provider(E2BProvider)
