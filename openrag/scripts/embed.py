#!/usr/bin/env python3

import sys
import os
import json
import time
import logging

from typing import IO, Any, Dict, List, Optional, Set, Tuple


class Embedder:
    """
    A wrapper around an OpenAI-compatible embedding endpoint with adaptive batching.

    Attributes:
        client:              OpenAI client instance.
        model_name:          Name of the embedding model.
        batch_size:          Fixed batch size if > 0, otherwise adaptive.
        max_batch_size:      Upper bound for adaptive batch size.
        curr_batch_size:     Current adaptive batch size being tested.
        durations:           Timing history for different batch sizes.
        max_duration_items:  Number of duration samples stored per batch size.
        num_outliers:        Number of outliers to remove before averaging timings.
    """

    def __init__(
        self,
        url: str,
        key: str,
        model_name: str,
        batch_size: int,
        logger: Any,
        verbose: bool = False):
        """
        Initialize an Embedder instance.

        Args:
            url:         URL to the OpenAI-compatible embedding service.
            key:         API key for authentication.
            model_name:  Embedding model name.
            batch_size:  Fixed batch size (0 or less means auto-detect optimal).
            logger:      Logger instance.
            verbose:     If True, log detailed performance info.
        """
        from openai import OpenAI

        self.client = OpenAI(base_url=url, api_key=key)
        self.model_name = model_name
        self.batch_size = batch_size

        self.verbose = verbose
        self.logger = logger

        self.max_batch_size = 1024 * 4
        self.curr_batch_size = 1
        self.durations = { self.curr_batch_size: [] }
        self.max_duration_items = 12
        self.num_outliers = 2
        if self.batch_size <= 0:
            self.batch_size = None
            # We'll guess the best value


    def get_batch_size(self) -> int:
        """
        Get the current batch size.

        Returns:
            int: Batch size for the next embedding request. If batch size was set
                 to 0 initially, adaptively selects a size based on measured timings.
        """
        if self.batch_size is None:
            if len(self.durations[self.curr_batch_size]) >= self.max_duration_items:
                if self.curr_batch_size >= self.max_batch_size:
                    # We are ready to make a choice
                    self.calc_batch_size()
                    assert(self.batch_size is not None and self.batch_size > 0)
                    return self.batch_size
                self.curr_batch_size *= 2
            return self.curr_batch_size

        assert(self.batch_size is not None and self.batch_size > 0)
        return self.batch_size


    def calc_batch_size(self) -> None:
        """
        Compute the optimal batch size based on recorded durations.

        Uses mean time per item (excluding outliers) for each tested batch size
        and picks the one with the lowest average.

        """
        data = []
        for k, v in self.durations.items():
            assert(len(v) >= self.max_duration_items)
            assert(int == type(k))

            v.sort()

            # remove outliers
            v = v[self.num_outliers:-self.num_outliers]
            mean = float(sum(v)) / len(v)
            data.append({ 'batch_size': k, 'time_per_item': mean / k })

            if self.verbose:
                self.logger.info(f'calc_batch_size: batch_size={k} time_per_item={mean/k:.4f} mean={mean:.4f}\n')

        data.sort(key=lambda item: item['time_per_item'])
        self.batch_size = data[0]['batch_size']


    def embed(self, data: list) -> list:
        """
        Generate embeddings for a batch of input texts.

        Args:
            data: List of strings to embed.

        Returns:
            List of embeddings (lists of floats).

        """
        before = time.time()
        response = self.client.embeddings.create(
            model=self.model_name,
            input=data
        )

        elapsed = time.time() - before
        l = len(data)
        if l not in self.durations:
            self.durations[l] = []
        elif len(self.durations[l]) >= self.max_duration_items:
            self.durations[l].pop(0)
        self.durations[l].append(elapsed)

        return [ item.embedding for item in response.data ]


def call_embedder_and_save(
        embedder: Embedder,
        ofh: IO[str],
        batch: list,
        text_field_name: str,
        vector_field_name: str,
        logger: Any,
        verbose: bool = False):
    """
    Call the embedder on a batch of items and save the results.

    Args:
        embedder:           Embedder instance.
        ofh:                Output file handle.
        batch:              List of JSON objects.
        text_field_name:    Key holding the text to embed.
        vector_field_name:  Key where the embedding will be stored.
        logger:             Logger instance.
        verbose:            If True, logs extra info.

    """
    embd = embedder.embed([ item[text_field_name] for item in batch ])

    for i in range(len(batch)):
        batch[i][vector_field_name] = embd[i]
        ofh.write(json.dumps(batch[i], ensure_ascii=False, sort_keys=True) + '\n')


def read_vdb_section(
        ifh: IO[str],
        ofh: IO[str],
        embedder: Any,
        logger: Any,
        text_field_name: str = 'text',
        vector_field_name: str = 'vector',
        verbose: bool = False,
    ) -> None:
    """
    Reads chunks, deletes old vectors and creates new ones with given embedder.

    Parameters:
        ifh:               Input backup file handle (already open for reading).
        ofh:               Output backup file handle (already open for writing).
        embedder:          Wrapper object around embedding model.
        logger:            Logger for status and error reporting.
        text_field_name    Name of the field with text to embed.
        vector_field_name  Name of the field to store vector.
        verbose:           If True, logs additional info.
    """
    if verbose:
        logger.info(f'Read vdb section')

    batch_size = embedder.get_batch_size()

    batch = []
    cnt = 0
    for line in ifh:
        # End of section
        if 0 == len(line):
            break

        if len(batch) >= batch_size:
            call_embedder_and_save(embedder, ofh, batch, text_field_name, vector_field_name, logger, verbose)
            batch = []
            batch_size = embedder.get_batch_size()

        chunk = json.loads(line)

        chunk.pop('_id', None)
        chunk.pop(vector_field_name, None)
        batch.append(chunk)

    if len(batch) > 0:
        call_embedder_and_save(embedder, ofh, batch, text_field_name, vector_field_name, logger, verbose)


def open_input_file(
        file_name: str,
        logger: Any
    ) -> IO[str]:
    """
    Opens a input file for reading, with support for plain text and LZMA-compressed (.xz) files.

    Parameters:
        file_name:  Path to the backup file.
        logger:     Logger for status and error reporting.

    Returns:
        file object:  Opened file handle in text mode.
    """
    if file_name in [ '-' ]:
        return sys.stdin

    try:
        if file_name.endswith('.xz'):
            import lzma
            return lzma.open(file_name, 'rt', encoding='utf-8')
        else:
            return open(file_name, 'rt', encoding='utf-8')
    except Exception as e:
        logger.error(f'Failed while opening file \'{file_name}\' for reading:\n' + str(e))
        raise


def open_output_file(
        file_name: str,
        logger: Any
    ) -> IO[str]:
    """
    Opens output file for writing

    Parameters:
        file_name:  Path to the output file or '-' for STDOUT
        logger:     Logger for status and error reporting.

    Returns:
        file object:  Opened file handle in text mode.
    """
    if file_name in [ '-' ]:
        return sys.stdout

    if os.path.isfile(file_name):
        raise Exception(f'File \'{file_name}\' already exists.')

    try:
        if file_name.endswith('.xz'):
            import lzma
            return lzma.open(file_name, 'wt', encoding='utf-8', preset=9 | lzma.PRESET_EXTREME)
        else:
            return open(file_name, 'wt', encoding='utf-8')
    except Exception as e:
        logger.error(f'Failed while opening file \'{file_name}\' for writing:\n' + str(e))
        raise


def close_file(
        file_handle: IO[str],
        file_name: str,
        logger: Any
    ) -> None:
    """
    Depending of the file name closes the file handle or does nothing

    Parameters:
        file_handle:  File handle presumably opened
        file_name:    The corresponding file name
        logger:       Logging for status and error reporting

    Returns:
        Nothing
    """
    if file_name in [ '-' ] or file_handle is None:
        return

    try:
        file_handle.close()
    except Exception as e:
        logger.exception(f'Failed to close file \'{file_name}\': ' + str(e))
        raise


def main():
    """
    Main entry point:
      - Parses CLI arguments.

    Parameters:
        None (arguments are parsed from sys.argv)

    Returns:
        int: Exit code (0 on success, non-zero on failure).
    """


    # Arguments and configs
    import argparse
    parser = argparse.ArgumentParser(description='OpenRAG embed tool')
    parser.add_argument('-b', '--batch-size', default=0, type=int, help='Batch size (0 - guess optimal batch size)')
    parser.add_argument('-v', '--verbose', default=False, action='store_true', help='Be verbose')
    parser.add_argument('-i', '--input', default='-', type=str, help='Input file name (\'-\' for STDIN)')
    parser.add_argument('-o', '--output', default='-', type=str, help='Output file name (\'-\' for STDOUT)')
    parser.add_argument('-u', '--url', type=str, help='URL to embedder OpenAI compatible endpoint')
    parser.add_argument('-k', '--key', type=str, help='Secret key to access embedder')
    parser.add_argument('-m', '--model', required=True, type=str, help='Model name')

    args = parser.parse_args()

    logger = logging.getLogger(__name__)


    try:
        embedder = Embedder(args.url, args.key, args.model, args.batch_size, logger, args.verbose)
    except Exception as e:
        logger.exception(f'Failed while trying to create embedder: ' + str(e))
        raise

    try:
        ifh, ofh = None, None
        ifh = open_input_file(args.input, logger)
        ofh = open_output_file(args.output, logger)

        for line in ifh:
            ofh.write(line)

            if line.strip() in [ 'vdb' ]:
                read_vdb_section(ifh, ofh, embedder, logger, 'text', 'vector', args.verbose)

    except Exception as e:
        logger.exception(f'Error: ' + str(e))
        raise
    finally:
        close_file(ifh, args.input, logger)
        close_file(ofh, args.output, logger)


if __name__ == '__main__':
    sys.exit(main())

