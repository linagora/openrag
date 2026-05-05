# Re-export from canonical location for backward compatibility.
# New code should import from `core.utils.text` directly.
from core.utils.text import (  # noqa: F401
    clean_markdown_table_spacing,
    sanitize_extracted_text,
    sanitize_text,
)
