# # exceptions.py
# from openrag.utils.exceptions import OpenRAGException


# class LoaderException(OpenRAGException):
#     """Base exception for all loader-related errors"""
#     pass


# class FileLoadingError(LoaderException):
#     """Raised when file reading/loading fails"""
#     def __init__(self, message: str, file_path: str = None, error_code: str = None):
#         self.file_path = file_path
#         self.error_code = error_code or "FILE_LOADING_ERROR"
#         super().__init__(f"File loading error: {message}")


# class ImageProcessingError(LoaderException):
#     """Raised when image processing fails"""
#     def __init__(self, message: str, image_url: str = None, error_code: str = None):
#         self.image_url = image_url
#         self.error_code = error_code or "IMAGE_PROCESSING_ERROR"
#         super().__init__(f"Image processing error: {message}")


# class InvalidFileFormatError(LoaderException):
#     """Raised when file format is invalid or unsupported"""
#     def __init__(self, message: str, file_path: str = None, error_code: str = None):
#         self.file_path = file_path
#         self.error_code = error_code or "INVALID_FILE_FORMAT"
#         super().__init__(f"Invalid file format: {message}")


# class EncodingError(LoaderException):
#     """Raised when file encoding issues occur"""
#     def __init__(self, message: str, file_path: str = None, error_code: str = None):
#         self.file_path = file_path
#         self.error_code = error_code or "ENCODING_ERROR"
#         super().__init__(f"Encoding error: {message}")
