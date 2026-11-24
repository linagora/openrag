import pytest

# The suite's assertions live outside a test_ module: have pytest rewrite them
# too, so a failure shows the values compared.
pytest.register_assert_rewrite("object_store_conformance")
