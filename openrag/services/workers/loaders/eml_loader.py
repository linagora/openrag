import datetime
import email
import io
import os
import tempfile
from email.utils import parsedate_to_datetime
from pathlib import Path

from langchain_core.documents.base import Document
from PIL import Image

from . import get_loader_classes
from .base import BaseLoader


def json_serial(obj):
    if isinstance(obj, datetime.datetime):
        serial = obj.isoformat()
        return serial


class EmlLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Store all kwargs for passing to sub-loaders
        self.kwargs = kwargs
        # Get available loaders for processing attachments
        self.loader_classes = get_loader_classes(config=self.config)

    async def aload_document(self, file_path, metadata: dict | None = None, save_markdown: bool = False):
        try:
            with open(file_path, "rb") as fhdl:
                raw_email = fhdl.read()

            # Parse email using standard email library
            email_msg = email.message_from_bytes(raw_email)

            # Extract email metadata
            email_data = {
                "header": {
                    "subject": email_msg.get("subject", ""),
                    "from": email_msg.get("from", ""),
                    "to": email_msg.get("to", ""),
                    "date": email_msg.get("date", ""),
                    "message-id": email_msg.get("message-id", ""),
                },
                "body": [],
                "attachment": [],
            }

            # Parse date if available
            if email_data["header"]["date"]:
                try:
                    email_data["header"]["date"] = parsedate_to_datetime(email_data["header"]["date"]).isoformat()
                except Exception:
                    pass

            # Extract body content and attachments
            body_content = ""

            for part in email_msg.walk():
                content_type = part.get_content_type()
                content_disposition = part.get_content_disposition()

                if content_disposition == "attachment" or content_disposition == "inline":
                    # Handle attachments
                    filename = part.get_filename()
                    if filename:
                        payload = part.get_payload(decode=True)
                        if payload:
                            attachment_info = {
                                "filename": filename,
                                "content_type": content_type,
                                "size": len(payload),
                                "raw": payload,
                            }
                            email_data["attachment"].append(attachment_info)

                elif content_type.startswith("text/"):
                    # Handle text content
                    if content_type == "text/plain" or content_type == "text/html":
                        text_content = part.get_payload(decode=True)
                        if text_content:
                            try:
                                # Try to decode as UTF-8, fallback to latin-1
                                if isinstance(text_content, bytes):
                                    try:
                                        text_content = text_content.decode("utf-8")
                                    except UnicodeDecodeError:
                                        text_content = text_content.decode("latin-1", errors="ignore")

                                body_info = {
                                    "content": text_content,
                                    "content_type": content_type,
                                }
                                email_data["body"].append(body_info)

                                # Use plain text as primary body content
                                if content_type == "text/plain" or not body_content:
                                    body_content = text_content
                            except Exception as e:
                                print(f"Failed to decode text content: {e}")

            # Extract body content
            content_body = body_content.strip() if body_content else ""

            # Process attachments using appropriate loaders
            attachments_text = ""
            if email_data["attachment"]:
                attachments_text = "\n\n--- ATTACHMENTS ---\n"
                for attachment in email_data["attachment"]:
                    filename = attachment.get("filename", "unknown")
                    content_type = attachment.get("content_type", "unknown")
                    size = attachment.get("size", "unknown")

                    attachments_text += f"\nAttachment: {filename}\n"
                    attachments_text += f"Content-Type: {content_type}\n"
                    attachments_text += f"Size: {size} bytes\n"

                    # Try to process attachment using appropriate loader
                    if "raw" in attachment:
                        try:
                            # Get file extension from filename
                            file_ext = Path(filename).suffix.lower()

                            # Check if we have a loader for this file type
                            loader_cls = self.loader_classes.get(file_ext)

                            if loader_cls:
                                # Save attachment to temporary file
                                with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
                                    temp_file.write(attachment["raw"])
                                    temp_file_path = temp_file.name

                                try:
                                    # Use appropriate loader to process attachment
                                    loader = loader_cls(**self.kwargs)
                                    attachment_doc = await loader.aload_document(
                                        temp_file_path,
                                        metadata={"source": f"attachment:{filename}"},
                                    )
                                    attachments_text += f"Content:\n{attachment_doc.page_content}\n"
                                except Exception as e:
                                    attachments_text += f"Failed to process attachment with loader ({loader_cls.__name__}): {str(e)[:200]}...\n"

                                    # Special fallback handling for PDFs with alternative loaders
                                    if file_ext == ".pdf":
                                        pdf_fallback_loaders = [
                                            "PyMuPDFLoader",
                                            "PyMuPDF4LLMLoader",
                                            "DoclingLoader",
                                        ]
                                        fallback_success = False

                                        for fallback_loader_name in pdf_fallback_loaders:
                                            if (
                                                fallback_loader_name != loader_cls.__name__
                                            ):  # Don't try the same loader again
                                                try:
                                                    # Try to get the fallback loader class
                                                    fallback_loader_cls = None
                                                    for (
                                                        ext,
                                                        cls,
                                                    ) in self.loader_classes.items():
                                                        if cls.__name__ == fallback_loader_name:
                                                            fallback_loader_cls = cls
                                                            break

                                                    if fallback_loader_cls:
                                                        attachments_text += (
                                                            f"Trying fallback PDF loader: {fallback_loader_name}\n"
                                                        )
                                                        fallback_loader = fallback_loader_cls(**self.kwargs)
                                                        attachment_doc = await fallback_loader.aload_document(
                                                            temp_file_path,
                                                            metadata={"source": f"attachment:{filename}"},
                                                        )
                                                        attachments_text += f"Content (via {fallback_loader_name}):\n{attachment_doc.page_content}\n"
                                                        fallback_success = True
                                                        break
                                                except Exception as fallback_e:
                                                    attachments_text += f"Fallback {fallback_loader_name} also failed: {str(fallback_e)[:100]}...\n"

                                        if not fallback_success:
                                            attachments_text += f"All PDF loaders failed for {filename}\n"

                                    # Try fallback processing for images
                                    if file_ext in [".png", ".jpg", ".jpeg", ".svg"]:
                                        try:
                                            if self.image_captioning:
                                                # Try to load image directly from bytes as fallback
                                                image = Image.open(io.BytesIO(attachment["raw"]))
                                                caption = await self.get_image_description(image_data=image)
                                                attachments_text += f"Fallback Image Description:\n{caption}\n"
                                            else:
                                                attachments_text += (
                                                    "Image attachment present but image captioning disabled\n"
                                                )
                                        except Exception as img_e:
                                            attachments_text += f"Image fallback also failed: {str(img_e)[:100]}...\n"

                                    # Try text fallback for other text-based formats
                                    elif file_ext in [".txt", ".docx", ".doc"] or (
                                        file_ext == ".pdf" and not fallback_success
                                    ):
                                        try:
                                            # Try to extract any readable text directly
                                            text_content = attachment["raw"].decode("utf-8", errors="ignore")
                                            if text_content.strip():
                                                attachments_text += (
                                                    f"Fallback text extraction:\n{text_content[:1000]}...\n"
                                                )
                                            else:
                                                attachments_text += "No readable text found in attachment\n"
                                        except Exception as text_e:
                                            attachments_text += f"Text fallback failed: {str(text_e)[:100]}...\n"
                                finally:
                                    # Clean up temporary file
                                    if os.path.exists(temp_file_path):
                                        os.unlink(temp_file_path)

                            # Special handling for images with captioning if no specific loader or captioning is enabled
                            elif (
                                file_ext
                                in [
                                    ".png",
                                    ".jpg",
                                    ".jpeg",
                                    ".svg",
                                ]
                                and self.image_captioning
                            ):
                                try:
                                    # Load image from raw bytes
                                    image = Image.open(io.BytesIO(attachment["raw"]))
                                    # Verify image can be processed
                                    image.verify()
                                    # Reopen image since verify() closes it
                                    image = Image.open(io.BytesIO(attachment["raw"]))
                                    # Generate caption using the base loader's method
                                    caption = await self.get_image_description(image_data=image)
                                    attachments_text += f"Image Description:\n{caption}\n"
                                except Exception as e:
                                    attachments_text += f"Failed to generate image caption: {str(e)[:200]}...\n"
                                    # Try to show basic image info if available
                                    try:
                                        size_info = f"Image size: {len(attachment['raw'])} bytes"
                                        attachments_text += (
                                            f"Image attachment present but corrupted or unreadable. {size_info}\n"
                                        )
                                    except Exception:
                                        attachments_text += "Image attachment present but corrupted or unreadable\n"

                            elif content_type.startswith("text/"):
                                # For text attachments, decode directly
                                attachment_content = attachment["raw"].decode("utf-8", errors="ignore")
                                attachments_text += f"Content:\n{attachment_content}\n"
                            else:
                                # For other binary content, just show metadata
                                attachments_text += f"Binary content (size: {len(attachment['raw'])} bytes)\n"
                        except Exception as e:
                            attachments_text += f"Content could not be processed: {e}\n"
                    attachments_text += "---\n"

            # Combine body and attachments
            content_body = content_body + attachments_text

            # Prepare metadata
            if metadata is None:
                metadata = {}

            # Add email metadata to document metadata
            metadata.update(
                {
                    "email_subject": email_data["header"]["subject"],
                    "email_from": email_data["header"]["from"],
                    "email_to": email_data["header"]["to"],
                    "email_date": email_data["header"]["date"],
                    "email_message_id": email_data["header"]["message-id"],
                    "email_attachment_count": len(email_data["attachment"]),
                    "email_attachment_filenames": [att["filename"] for att in email_data["attachment"]],
                }
            )

            # Add attachment metadata if there are attachments
            if email_data["attachment"]:
                attachment_metadata = []
                for att in email_data["attachment"]:
                    attachment_metadata.append(
                        {
                            "filename": att["filename"],
                            "content_type": att["content_type"],
                            "size": att["size"],
                        }
                    )
                metadata["email_attachments"] = attachment_metadata

            # Save content body to a file if requested
            if save_markdown:
                markdown_path = Path(file_path).with_suffix(".md")
                with open(markdown_path, "w", encoding="utf-8") as md_file:
                    md_file.write(content_body)
                metadata["markdown_path"] = str(markdown_path)
        except Exception as e:
            raise ValueError(f"Failed to parse the EML file {file_path}: {e}")

        document = Document(page_content=content_body, metadata=metadata)
        return document
