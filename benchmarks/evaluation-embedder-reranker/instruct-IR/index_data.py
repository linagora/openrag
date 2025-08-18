from io import BytesIO
import itertools
import csv
from pathlib import Path
import json
from typing import Generator, TypedDict
import httpx
import asyncio
import os
from tqdm.asyncio import tqdm
import math


class WikiPage(TypedDict):
    id: str
    text: str

data_path = "./"
data_path = Path(data_path)
AUTH_KEY = "sk-ragdondin-1234"

headers = {
    "accept": "application/json",
    "Authorization": f"Bearer {AUTH_KEY}",
}

def get_pages(
    wiki_dump_name: str, n: int = math.inf, filter_func=None
) -> Generator[WikiPage, None, None]:
    for file in data_path.glob(f"./data/corpus.csv"):
        with open(file, "r", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for i, row in enumerate(reader):
                # Ensure keys match WikiPage fields
                page = {"id": row.get("id", ""), "text": row.get("text", "")}
                if filter_func and filter_func(page):
                    yield page

                if i >= n:
                    break


def nword_greater(text: str, nword: int = 16) -> bool:
    words_l = text["text"].split()
    return len(words_l) > nword


# check if api is up with the /health_check endpoint
def check_api(base_url):
    try:
        response = httpx.get(base_url + "health_check")
        if response.status_code == 200:
            print("API is up and running")
        else:
            print("API is down")
    except httpx.RequestError as e:
        print(f"An error occurred: {e}")


async def check_file(client: httpx.AsyncClient, checking_url: str):
    try:
        response = await client.get(checking_url)

        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            return False

    except httpx.RequestError as e:
        print(f"Request error: {e}")
        return False


async def upload_file(client, semaphore, data, base_url, partition_name):
    temp_file_path = None
    try:
        content = data.pop("text")
        article_no = data['id']

        if not content:
            print(f"No content for title: {article_no}")
            return

        # Use in-memory file-like object
        file_obj = BytesIO(content.encode())

        # Construct URL
        file_id = data['id']
        url = f"{base_url}indexer/partition/{partition_name}/file/{file_id}"

        # Async JSON serialization
        loop = asyncio.get_event_loop()
        metadata = await loop.run_in_executor(None, json.dumps, data)

        # Use semaphore to limit concurrent uploads
        async with semaphore:
            files = {
                "file": (f"{article_no}.txt", file_obj, "text/plain"),
                "metadata": (None, metadata),
            }

            response = await client.post(
                url, files=files, headers=headers
            )

            if not response.is_success:
                print(f"Error uploading {article_no}: {response.status_code}")

    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    except KeyError as e:
        print(f"Missing key in data: {e}")
    except Exception as e:
        print(f"Unexpected error uploading {article_no}: {e}")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


async def main(pages, base_url, partition_name, n_semaphore=10):
    async with httpx.AsyncClient(timeout=60) as client:
        semaphore = asyncio.Semaphore(n_semaphore)
        batch_size = n_semaphore

        with tqdm(
            desc="Uploading files", unit="file", mininterval=5 * 60
        ) as progress_bar:
            while True:
                # Use regular islice for a standard generator
                batch = list(itertools.islice(pages, batch_size))
                if not batch:
                    break  # Exit when no more items

                tasks = [
                    upload_file(client, semaphore, data, base_url, partition_name)
                    for data in batch
                ]
                results = await asyncio.gather(*tasks)
                progress_bar.update(len(batch))  # Update after batch completes 


# Usage example
if __name__ == "__main__":
    num_port = os.environ.get("APP_PORT")
    num_host = "localhost"
    base_url = f"http://{num_host}:{num_port}/"

    # Get data
    wiki_dump_name = "benchmark"
    partition_name = wiki_dump_name

    check_api(base_url=base_url)
    n_semaphore = 8

    print("Loading Data")
    wiki_pages = get_pages(
        wiki_dump_name,
        n=1000,  
        filter_func=lambda x: nword_greater(x, nword=16),
    )

    asyncio.run(main(wiki_pages, base_url, partition_name, n_semaphore=n_semaphore))
