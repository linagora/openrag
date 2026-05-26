"""Legacy indexer package.

Import concrete classes from their modules directly. Keeping this package
initializer empty avoids import-time Ray/bootstrap side effects when callers
only need nested utility modules such as ``components.indexer.utils.files``.
"""

__all__: list[str] = []
