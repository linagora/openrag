import warnings

from config import load_config


def test_config_loads_successfully():
    """Verify config loads and returns expected structure."""
    config = load_config()

    assert config is not None
    assert hasattr(config, "llm")
    assert hasattr(config, "embedder")
    assert hasattr(config, "vectordb")
    assert hasattr(config, "paths")
    assert hasattr(config, "loader")


def test_config_critical_values_unchanged():
    """Verify critical config values match expected defaults."""
    config = load_config()

    # LLM settings
    assert config.llm.temperature == 0.1

    # Embedder settings
    assert config.embedder.provider == "openai"

    # Vectordb settings (note: hybrid_search is a string 'True' due to oc.env without oc.decode)
    assert config.vectordb.hybrid_search == 'True'

    # Loader settings (boolean because it uses oc.decode)
    assert config.loader.image_captioning is True


def test_config_no_hydra_version_warnings():
    """Verify no version_base warnings are emitted during config loading."""
    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")
        load_config()

        # Check no warnings contain "version_base"
        for warning in captured_warnings:
            assert "version_base" not in str(warning.message).lower(), \
                f"Unexpected version_base warning: {warning.message}"
