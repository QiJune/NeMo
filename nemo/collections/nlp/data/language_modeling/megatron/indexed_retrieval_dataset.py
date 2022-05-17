# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# Most of the code here has been copied from:
#   fairseq/fairseq/data/indexed_dataset.py

# with some modifications:

# Removed IndexedRawTextDataset since it relied on Fairseq dictionary
# other slight modifications to remove fairseq dependencies
# Added document index to index file and made it accessible.
#    An empty sentence no longer separates documents.

import os
import shutil
import struct
from functools import lru_cache
from itertools import accumulate

import numpy as np
import torch

from nemo.utils import logging

dtypes = {1: np.uint8, 2: np.int8, 3: np.int16, 4: np.int32, 5: np.int64, 6: np.float, 7: np.double, 8: np.uint16}


def code(dtype):
    for k in dtypes.keys():
        if dtypes[k] == dtype:
            return k
    raise ValueError(dtype)


def index_file_path(prefix_path):
    return prefix_path + '.idx'


def data_file_path(prefix_path):
    return prefix_path + '.bin'


def _warmup_mmap_file(path):
    with open(path, 'rb') as stream:
        while stream.read(100 * 1024 * 1024):
            pass


class MMapRetrievalIndexedDataset(torch.utils.data.Dataset):
    class Index(object):
        _HDR_MAGIC = b'MMIDRET\x00\x00'

        @classmethod
        def writer(cls, path, dtype, retrieval_db):
            class _Writer(object):
                def __enter__(self):
                    self._file = open(path, 'wb')

                    self._file.write(cls._HDR_MAGIC)
                    self._file.write(struct.pack('<Q', 1))
                    self._file.write(struct.pack('<B', code(dtype)))

                    return self

                @staticmethod
                def _get_pointers(sizes, chunk_size):
                    dtype_size = dtype().itemsize
                    address = 0
                    pointers = []

                    for size in sizes:
                        pointers.append(address)
                        address += size * dtype_size
                        if retrieval_db:
                            # if it is retrieval db, the the last chunk is reserved for padding
                            address += chunk_size * dtype_size
                    return pointers

                @staticmethod
                def _get_chunk_id_and_address(sizes, chunk_size):
                    dtype_size = dtype().itemsize
                    chunk_ids = []
                    last_id = 0
                    address = 0
                    pointers = []
                    for size in sizes:
                        chunk_ids.append(last_id)
                        num_of_chunks = size // chunk_size
                        if size % chunk_size != 0:
                            raise ValueError(f"the sentence size {size} should be the multiple of {chunk_size}")
                        for _ in range(num_of_chunks):
                            pointers.append(address)
                            address += chunk_size * dtype_size
                        if retrieval_db:
                            # if it is retrieval db, the the last chunk is reserved for padding
                            address += chunk_size * dtype_size
                        last_id += num_of_chunks
                    return chunk_ids, pointers

                def write(self, sizes, chunk_size):
                    pointers = self._get_pointers(sizes, chunk_size)
                    chunk_ids, chunk_address = self._get_chunk_id_and_address(sizes, chunk_size)

                    self._file.write(struct.pack('<Q', len(sizes)))
                    self._file.write(struct.pack('<Q', chunk_size))
                    self._file.write(struct.pack('<Q', len(chunk_address)))
                    self._file.write(struct.pack('<B', int(retrieval_db)))

                    sizes = np.array(sizes, dtype=np.int32)
                    self._file.write(sizes.tobytes(order='C'))
                    del sizes

                    pointers = np.array(pointers, dtype=np.int64)
                    self._file.write(pointers.tobytes(order='C'))
                    del pointers

                    chunk_ids = np.array(chunk_ids, dtype=np.int64)
                    self._file.write(chunk_ids.tobytes(order='C'))
                    del chunk_ids

                    chunk_address = np.array(chunk_address, dtype=np.int64)
                    self._file.write(chunk_address.tobytes(order='C'))

                def __exit__(self, exc_type, exc_val, exc_tb):
                    self._file.close()

            return _Writer()

        def __init__(self, path, skip_warmup=False):
            with open(path, 'rb') as stream:
                magic_test = stream.read(9)
                assert self._HDR_MAGIC == magic_test, (
                    'Index file doesn\'t match expected format. '
                    'Make sure that --dataset-impl is configured properly.'
                )
                version = struct.unpack('<Q', stream.read(8))
                assert (1,) == version

                (dtype_code,) = struct.unpack('<B', stream.read(1))
                self._dtype = dtypes[dtype_code]
                self._dtype_size = self._dtype().itemsize

                self._len = struct.unpack('<Q', stream.read(8))[0]
                self.chunk_size = struct.unpack('<Q', stream.read(8))[0]
                self.num_chunks = struct.unpack('<Q', stream.read(8))[0]
                self.retrieval_db = bool(struct.unpack('<B', stream.read(1))[0])
                # self.chunk_size = struct.unpack('<Q', stream.read(8))[0]
                # self.num_chunks = struct.unpack('<Q', stream.read(8))[0]
                offset = stream.tell()

            if not skip_warmup:
                logging.info("    warming up index mmap file...")
                _warmup_mmap_file(path)

            self._bin_buffer_mmap = np.memmap(path, mode='r', order='C')
            self._bin_buffer = memoryview(self._bin_buffer_mmap)
            logging.info("    reading sentences sizes...")
            self._sizes = np.frombuffer(self._bin_buffer, dtype=np.int32, count=self._len, offset=offset)
            logging.info("    reading sentences pointers...")
            self._pointers = np.frombuffer(
                self._bin_buffer, dtype=np.int64, count=self._len, offset=offset + self._sizes.nbytes
            )
            logging.info("    reading sentence chunk offset...")
            self._chunk_id_start = np.frombuffer(
                self._bin_buffer,
                dtype=np.int64,
                count=self._len,
                offset=offset + self._sizes.nbytes + self._pointers.nbytes,
            )
            logging.info("    reading chunk address...")
            self._chunk_address = np.frombuffer(
                self._bin_buffer,
                dtype=np.int64,
                count=self.num_chunks,
                offset=offset + self._sizes.nbytes + self._pointers.nbytes + self._chunk_id_start.nbytes,
            )

        def get_chunk_address(self, chunk_id):
            """ get the chunk address from chunk id
            """
            return self._chunk_address[chunk_id]

        def get_chunk_id(self, sentence_id, position):
            """ get the chunk id from sentence idx and offset position.
            """
            return (self._chunk_id_start[sentence_id] + position // self.chunk_size).item()

        def __del__(self):
            self._bin_buffer_mmap._mmap.close()
            del self._bin_buffer_mmap

        @property
        def dtype(self):
            return self._dtype

        @property
        def sizes(self):
            return self._sizes

        @lru_cache(maxsize=8)
        def get_chunk(self, chunk_id):
            return self._pointers[i], self._sizes[i]

        @lru_cache(maxsize=8)
        def __getitem__(self, i):
            return self._pointers[i], self._sizes[i]

        def __len__(self):
            return self._len

    def __init__(self, path, skip_warmup=False):
        super().__init__()

        self._path = None
        self._index = None
        self._bin_buffer = None

        self._do_init(path, skip_warmup)

    def __getstate__(self):
        return self._path

    # def __setstate__(self, state):
    #     self._do_init(state)

    def _do_init(self, path, skip_warmup):
        self._path = path
        self._index = self.Index(index_file_path(self._path), skip_warmup)

        if not skip_warmup:
            logging.info("    warming up data mmap file...")
            _warmup_mmap_file(data_file_path(self._path))
        logging.info("    creating numpy buffer of mmap...")
        self._bin_buffer_mmap = np.memmap(data_file_path(self._path), mode='r', order='C')
        logging.info("    creating memory view of numpy buffer...")
        self._bin_buffer = memoryview(self._bin_buffer_mmap)

    def __del__(self):
        self._bin_buffer_mmap._mmap.close()
        del self._bin_buffer_mmap
        del self._index

    def __len__(self):
        return len(self._index)

    # @lru_cache(maxsize=8)
    def __getitem__(self, idx):
        if isinstance(idx, int):
            ptr, size = self._index[idx]
            np_array = np.frombuffer(self._bin_buffer, dtype=self._index.dtype, count=size, offset=ptr)
            return np_array
        elif isinstance(idx, slice):
            start, stop, step = idx.indices(len(self))
            if step != 1:
                raise ValueError("Slices into indexed_dataset must be contiguous")
            ptr = self._index._pointers[start]
            if self._index.retrieval_db:
                sizes = self._index._sizes[idx] + self._index.chunk_size
            else:
                sizes = self._index._sizes[idx]
            offsets = list(accumulate(sizes))
            total_size = sum(sizes)
            np_array = np.frombuffer(self._bin_buffer, dtype=self._index.dtype, count=total_size, offset=ptr)
            sents = np.split(np_array, offsets[:-1])
            if self._index.retrieval_db:
                sents = [sent[: -self._index.chunk_size] for sent in sents]
            return sents

    def get(self, idx, offset=0, length=None):
        """ Retrieves a single item from the dataset with the option to only
        return a portion of the item.

        get(idx) is the same as [idx] but get() does not support slicing.
        """
        ptr, size = self._index[idx]
        if length is None:
            length = size - offset
        ptr += offset * np.dtype(self._index.dtype).itemsize
        np_array = np.frombuffer(self._bin_buffer, dtype=self._index.dtype, count=length, offset=ptr)
        return np_array

    def get_chunk_id(self, idx, offset=0):
        """ get the chunk id from sentence idx and offset position.
        """
        # make sure offset is a multiple of chunk_size
        assert offset % self._index.chunk_size == 0
        return self._index.get_chunk_id(idx, offset)

    def get_chunk(self, chunk_id, force_no_padding=False):
        """ Retrieves a single chunk item from the dataset.
        """
        if isinstance(chunk_id, (int, np.int64, np.int32)):
            ptr = self._index.get_chunk_address(chunk_id)
            if self._index.retrieval_db:
                size = self._index.chunk_size * 2
            else:
                size = self._index.chunk_size
            np_array = np.frombuffer(self._bin_buffer, dtype=self._index.dtype, count=size, offset=ptr)
            return np_array
        elif isinstance(chunk_id, slice):
            start, stop, step = chunk_id.indices(self.chunks)
            if step != 1:
                raise ValueError("Slices into indexed_dataset must be contiguous")
            if self._index.retrieval_db and (not force_no_padding):
                chunk_size = self._index.chunk_size * 2
            else:
                chunk_size = self._index.chunk_size
            ptr = self._index.get_chunk_address(start)
            end_address = self._index.get_chunk_address(stop - 1) + chunk_size * self._index._dtype_size
            address = self._index._chunk_address[chunk_id]
            starting_pos = address // self._index._dtype_size
            total_size = (end_address - ptr) // self._index._dtype_size
            np_array = np.frombuffer(self._bin_buffer, dtype=self._index.dtype, count=total_size, offset=ptr)
            sents = [np_array[pos : pos + chunk_size] for pos in starting_pos - starting_pos[0]]
            return sents

    @property
    def sizes(self):
        return self._index.sizes

    @property
    def chunks(self):
        return self._index.num_chunks

    @property
    def chunk_size(self):
        return self._index.chunk_size

    @property
    def supports_prefetch(self):
        return False

    @staticmethod
    def exists(path):
        return os.path.exists(index_file_path(path)) and os.path.exists(data_file_path(path))


class MMapRetrievalIndexedDatasetBuilder(object):
    def __init__(self, out_file, chunk_size, pad_id, retrieval_db=False, dtype=np.int64):
        self._data_file = open(out_file, 'wb')
        self._dtype = dtype
        self.chunk_size = chunk_size
        self._sizes = []
        self.retrieval_db = retrieval_db
        self.pad_id = pad_id

    def add_item(self, tensor):
        np_array = np.array(tensor.numpy(), dtype=self._dtype)
        padded_size = self.chunk_size - (len(np_array) % self.chunk_size)
        data_size = np_array.size + padded_size
        if self.retrieval_db:
            # for retrieval database, added one more chunk in the end as padding
            padded_size += self.chunk_size
        np_array = np.pad(np_array, (0, padded_size), 'constant', constant_values=self.pad_id)
        self._data_file.write(np_array.tobytes(order='C'))
        self._sizes.append(data_size)

    def end_document(self):
        pass

    def merge_file_(self, another_file):
        # Concatenate index
        index = MMapRetrievalIndexedDataset.Index(index_file_path(another_file))
        assert index.dtype == self._dtype

        for size in index.sizes:
            self._sizes.append(size)

        # Concatenate data
        with open(data_file_path(another_file), 'rb') as f:
            shutil.copyfileobj(f, self._data_file)

    def finalize(self, index_file):
        self._data_file.close()

        with MMapRetrievalIndexedDataset.Index.writer(index_file, self._dtype, self.retrieval_db) as index:
            index.write(self._sizes, self.chunk_size)