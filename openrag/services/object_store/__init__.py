"""Object-store adapters for off-Ray file handoff (in-memory, S3/MinIO).

Deliberately a light package (no eager heavy imports) so importing an adapter
never drags in the vector/RDB stores.
"""
