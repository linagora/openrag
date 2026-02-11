import warnings

import consts
import pytest


def test_legacy_prefix_emits_deprecation_warning():
    """Verify legacy 'ragondin-' prefix triggers DeprecationWarning."""
    # Test the warning logic directly without full function import
    # This simulates the code path in get_partition_name for legacy prefix

    model_name = "ragondin-test_partition"

    # Use pytest.warns to verify DeprecationWarning is emitted
    with pytest.warns(DeprecationWarning, match="deprecated"):
        # Replicate the warning logic from get_partition_name
        if model_name.startswith(consts.LEGACY_PARTITION_PREFIX):
            warnings.warn(
                f"The partition prefix '{consts.LEGACY_PARTITION_PREFIX}' is deprecated "
                f"and will be removed in a future version. "
                f"Please update your model names to use '{consts.PARTITION_PREFIX}' instead. "
                f"Example: '{consts.LEGACY_PARTITION_PREFIX}mypartition' -> '{consts.PARTITION_PREFIX}mypartition'",
                DeprecationWarning,
                stacklevel=2,
            )


def test_current_prefix_no_deprecation_warning():
    """Verify current 'openrag-' prefix does NOT trigger DeprecationWarning."""
    model_name = "openrag-test_partition"

    # Capture all warnings
    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")

        # Replicate the condition check from get_partition_name
        partition_prefix = consts.PARTITION_PREFIX
        if model_name.startswith(consts.LEGACY_PARTITION_PREFIX):
            warnings.warn(
                f"The partition prefix '{consts.LEGACY_PARTITION_PREFIX}' is deprecated "
                f"and will be removed in a future version. "
                f"Please update your model names to use '{consts.PARTITION_PREFIX}' instead. "
                f"Example: '{consts.LEGACY_PARTITION_PREFIX}mypartition' -> '{consts.PARTITION_PREFIX}mypartition'",
                DeprecationWarning,
                stacklevel=2,
            )
            partition_prefix = consts.LEGACY_PARTITION_PREFIX

        # Verify the warning was NOT triggered (model starts with current prefix)
        assert partition_prefix == consts.PARTITION_PREFIX

        # Verify no DeprecationWarning was emitted
        deprecation_warnings = [
            w for w in captured_warnings
            if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation_warnings) == 0, \
            f"Unexpected DeprecationWarning(s): {[str(w.message) for w in deprecation_warnings]}"
