from importlib.metadata import version as installed_version

from cfxmark import __version__


def test_module_version_matches_distribution_metadata() -> None:
    assert __version__ == installed_version("cfxmark")
