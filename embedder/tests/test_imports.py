"""Smoke check: the embedder module imports cleanly in the test env.
If this fails the conftest env-var setup is wrong."""


def test_module_imports():
    import main  # noqa: F401
