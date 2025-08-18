import httpx
import os
from loguru import logger
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

num_port = os.environ.get("APP_PORT")
app_url = os.environ["APP_URL"]
base_url = f"http://{app_url}:{num_port}"  # the base url of your running app for instance: 'http://localhost:8080'

dir_name = "./pdf_files/terresunivia_pdfs"  # Replace with your directory path
dir_path = Path(dir_name).resolve()

def __check_api(base_url):
    try:
        response = httpx.get(f"{base_url}/health_check")
        if response.status_code == 200:
            logger.info("API is up and running")

    except httpx.RequestError as e:
        logger.debug(f"An error occurred: {e}")
        raise e


__check_api(base_url)

partition = input("Write the name of your partition: ")  # "benchmark"


print(dir_path.is_dir())

for file_path in dir_path.glob("**/*"):
    logger.info(f"file: {file_path}")
    if file_path.is_file():
        file_ext = file_path.suffix[1:]  # Get the file extension without the dot
        filename = file_path.name  # Get the filename without the directory path

        file_id = filename  # or generate a unique ID if necessary

        # file_id = str(uuid.uuid4())
        url_template = f"{base_url}/indexer/partition/{partition}/file/{file_id}"
        url = url_template.format(partition_name=partition, file_id=file_id)

        with open(file_path, "rb") as f:
            files = {
                "file": (filename, f, f"application/{file_ext}"),
                "metadata": (None, ""),
            }

            response = httpx.post(
                url, files=files, headers={"accept": "application/json"}
            )
            print(f"Uploaded {filename}: {response.status_code} - {response.text}")
