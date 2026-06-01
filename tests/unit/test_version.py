from importlib.metadata import version as get_package_version


def test_package_version_is_valid():
    """Test that the package version can be read from metadata."""
    version = get_package_version("openrag")
    assert version is not None
    assert isinstance(version, str)
    assert len(version) > 0


def test_package_version_format():
    """Test that the version follows semantic versioning format."""
    version = get_package_version("openrag")
    parts = version.split(".")
    assert len(parts) >= 2, "Version should have at least major.minor"
    # Check that major and minor are numeric
    assert parts[0].isdigit(), "Major version should be numeric"
    assert parts[1].isdigit(), "Minor version should be numeric"
