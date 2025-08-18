#!/usr/bin/env python3

import httpx
import argparse
from pathlib import Path
from loguru import logger


parser = argparse.ArgumentParser(description="Index documents from local file system")
parser.add_argument(
    "-u",
    "--url",
    default="http://localhost:8080",
    type=str,
    help="The base url of your OpenRAG instance",
)
parser.add_argument(
    "-a", "--auth", required=False, type=str, help="AUTH_KEY (see the .env.example"
)
parser.add_argument(
    "-d",
    "--dir",
    required=True,
    type=str,
    help="The location of the documents to index",
)
parser.add_argument(
    "-p", "--partition", required=True, type=str, help="Target partition"
)
args = parser.parse_args()

headers = {"accept": "application/json"}
if args.auth is not None and len(args.auth) > 0:
    headers["Authorization"] = f"Bearer {args.auth}"

dir_path = Path(args.dir).resolve()


def __check_api(base_url):
    try:
        response = httpx.get(f"{base_url}/health_check", headers=headers)
        if response.status_code == 200:
            logger.info("API is up and running")

    except httpx.RequestError as e:
        logger.debug(f"An error occurred: {e}")
        raise e


def __check_file_exists(base_url, partition, file_name, headers):
    try:
        url = f"{base_url}/partition/check-file/{partition}/file/{file_name}"
        response = httpx.get(url, headers=headers, timeout=60)
        if 200 == response.status_code:
            return True
    except Exception as e:
        logger.debug(f"An error occurred: {e}")
        raise e

    return False


__check_api(args.url)

print(dir_path.is_dir())

for file_path in dir_path.glob("**/*"):
    logger.info(f"file: {file_path}")
    if file_path.is_file():
        file_ext = file_path.suffix[1:]  # Get the file extension without the dot
        filename = file_path.name  # Get the filename without the directory path

        file_id = filename  # or generate a unique ID if necessary

        if __check_file_exists(args.url, args.partition, filename, headers):
            logger.info(f'"{filename}" exists')
            continue
        else:
            logger.info(f'"{filename}" doesn\'t exist')

        # file_id = str(uuid.uuid4())

        url_template = f"{args.url}/indexer/partition/{args.partition}/file/{file_id}"
        url = url_template.format(partition_name=args.partition, file_id=file_id)

        with open(file_path, "rb") as f:
            files = {
                "file": (filename, f, f"application/{file_ext}"),
                "metadata": (None, ""),
            }

            response = httpx.post(url, files=files, headers=headers, timeout=60)
            print(f"Uploaded {filename}: {response.status_code} - {response.text}")


# How to run this code:
# uv run python utility/data_indexer.py -d /path/to/your/documents -p your_partition_name -u http://localhost:8080 -a your_auth_key
