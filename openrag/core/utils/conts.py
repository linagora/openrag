PARTITION_PREFIX = "openrag-"
LEGACY_PARTITION_PREFIX = "ragondin-"

FILE_READ_CHUNK_SIZE = 1024 * 1024  # Read file in blocks of 1MB to preserve RAM


IMG_WRAPPER_OPEN = "<image_description>\n\n"
IMG_WRAPPER_CLOSE = "\n\n</image_description>"

IMAGE_PLACEHOLDER = f"""{IMG_WRAPPER_OPEN}[Image Placeholder]{IMG_WRAPPER_CLOSE}"""
