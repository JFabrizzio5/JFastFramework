"""The commands `main.py` used to define itself, one module per responsibility.

Each module exposes ``register(app)``, the same shape as the lifecycle commands
beside this package, and ``main.py`` calls them in the order ``jfast --help``
lists the commands.
"""
