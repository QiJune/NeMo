# Copyright 2018 The Google AI Language Team Authors and
# The HuggingFace Inc. team.
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

# Some code of this file was adapted from the HuggingFace library available at
# https://github.com/huggingface/transformers
import math
import os
import mmap
import glob
import pickle
import pyarrow as pa
from typing import Dict, Optional, List, Iterator, TypeVar, Callable
from multiprocessing import Lock

import torch
import numpy as np
from datasets import (
    load_dataset,
    load_from_disk,
    set_caching_enabled,
    interleave_datasets,
    arrow_dataset
)

from nemo.core.classes import Dataset
from nemo.utils.app_state import AppState
from nemo.collections.nlp.data.language_modeling.megatron.blendable_dataset import BlendableDataset
from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.nlp.data.language_modeling.t0_task_manager import (
    DATA_ORG, t0_all_evaldt_names_subset,
    get_data_paths_and_splits
)
from nemo.collections.nlp.data.language_modeling.t0_task_manager import (
    get_task_id,
    TEMPLATE_CHUNK_NAME,
    ORIG_TXT_CHUNK_NAME
)
from nemo.core.neural_types import NeuralType
from nemo.utils import logging

T_co = TypeVar('T_co', covariant=True)
mutex = Lock()

try:
    from apex.transformer import parallel_state
    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


class Task(object):
    def __init__(self, file_path: str,  dt_name: str, subset: str):
        self.file_path = file_path
        self.dt_name = dt_name
        self.subset = subset
        self.task_id = get_task_id(dt_name, subset)
        self.prompt_id = {}
        #self.dataset_size = self.mapcount()

    def mapcount(self):
        f = open(self.file_path, "r+")
        buf = mmap.mmap(f.fileno(), 0)
        lines = 0
        readline = buf.readline
        while readline():
            lines += 1
        return lines

    @staticmethod
    def create_example(task_id, prompt_id):
        raise "Task `create_example` not implemented"

    def tokenize(self, example):
        raise "Taks `tokenize` not implemented"

    def fn(self, multi_prompted_ex, split):
        features = {}
        for prompt_type, data in multi_prompted_ex.items():
            self.prompt_id[prompt_type] = self.prompt_id.get(prompt_type, len(self.prompt_id) + 1)
            if data is None:
                data = {'input': None, 'output': None, 'chunked_idx': None}
            example = self.create_example(data, self.task_id, self.prompt_id[prompt_type])
            tokenized_features = self.tokenize(example)
            feature_dicts = {f'{k}_{self.prompt_id[prompt_type]}': v for k, v in tokenized_features.items()}
            features.update(feature_dicts)
        return features

    def map_fn_train(self, multi_prompted_ex):
        """ Dictionary of promtps templates is flattened and template names are appended to dictionary keys.
        """
        features = self.fn(multi_prompted_ex, 'train')
        return features

    def map_fn_eval(self, multi_prompted_ex):
        """ Mapping is handled a differently for eval/test since we will not sample prompts.
            Features dict keys is generic and not composed with prompt template specific ids.
        """
        features = self.fn(multi_prompted_ex, 'test/eval')
        return features


class InputPromptedExample(object):
    """A single training/test example for prompted inputs.

    Args:
        task_id: Unique id for the example.
        text: The untokenized text of the first sequence.
        For single sequence tasks, only this sequence must be specified.
        prompt_type: Name of prompt applied to the exampled.
        label:The label of the example. This should be
        specified for train and dev examples, but not for test examples.
        chunked_idx: a prompt is already applied on original text,
        we can recover the template text using the idx
    """

    def __init__(self, task_id: int, text: str, prompt_id: int = None, label: str = None, chunked_idx: List[List[int]] = None):
        """Constructs a InputExample."""
        self.task_id = task_id
        self.input_text = text
        self.prompt_id = prompt_id
        self.label = label
        self.chunked_idx =chunked_idx

    def __repr__(self):
        return (
            f"InputExample(task_id='{self.task_id}', input_text='{self.input_text}', "
            f"prompt_type='{self.prompt_id}', label='{self.label}'), "
            f"template_idx='{self.template_idx}', original_text_idx={self.original_text_idx}."
        )


class T0HFDatasetBuilder(object):
    """T0 Dataset Builder in a text-to-text format."""

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return

    def __init__(
            self,
            t0_type: str,
            dir_path: str,
            max_sampling_size: int,
            split: str,
            tokenizer: TokenizerSpec,
            max_seq_length: int,
            max_seq_length_decoder: int = 128,
            seed: int = 43,
            use_cache: bool = True,
            distribute_datasets: bool = False,
            extension: str = 'json',
            max_samples: int = None,
            num_proc: int = None,
            num_gpus: int = None,
            num_nodes: int = 1
    ):
        """
        Processes T0 dataset
        Args:
            t0_type: [train_t0, train_t0p, train_t0pp, t0_debug]
            split: train, test, eval
            max_sampling_size: caps probabilities at max size for a specific dataset
            dir_path: path to data directory for train, evaluation or test
            tokenizer: such as AutoTokenizer
            max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
            max_seq_length_decoder: max sequence length
            buffer_size: size of the buffer, chunks of data to suffle
            use_cache: whether to use data cache
            max_samples: limit size of dataset (not implemented)
            num_proc: number of processes to load data
        """
        self.t0_type = t0_type
        self.dir_path = dir_path
        self.max_sampling_size = max_sampling_size
        self.split = split
        self.tokenizer = tokenizer
        self.max_query_length = max_seq_length
        self.max_query_length_decoder = max_seq_length_decoder
        self.seed = seed
        self.use_cache = use_cache
        self.distribute_datasets = distribute_datasets
        self.extension = extension
        self.max_samples = max_samples
        self.num_proc = num_proc if num_proc > 0 else None
        self.num_gpus = num_gpus
        self.num_nodes = num_nodes
        self.tasks = []
        self.empty_prompt_token_id = -1
        self.datasets = self.get_data_dict()

    def assemble_datasets(self):
        if self.split == 'train':
            datasets_list = list(self.datasets.values())
            datasets = interleave_datasets(
                datasets=datasets_list,
                probabilities=self.get_sampling_probs(),
                seed=self.seed
            )
            return datasets
        else:
            return self.datasets

    def map_dataset(self, task, rank, features_dir):
        logging.info('Waiting for main process to perform the mapping/preprocessing.')
        if rank == 0:
            dataset = load_dataset(
                self.extension, data_files=task.file_path, split='train'
            )
            if self.max_samples is not None:
                dataset = dataset.select(range(min(len(dataset), self.max_samples)))
            original_column_names = dataset.column_names
            dataset = dataset.map(
                function=task.map_fn_train if self.split == 'train' else task.map_fn_eval,
                batched=False,
                num_proc=self.num_proc,
                remove_columns=original_column_names,
            )
            dataset.save_to_disk(features_dir)
        torch.distributed.barrier()
        logging.info('Finished waiting for main process in map_dataset().')

    def distribute_dataset(self, rank, world_size, features_dir):
        existing_node_folders = glob.glob(features_dir + '/node*')
        if len(existing_node_folders) != self.num_nodes and rank == 0:
            logging.info('Waiting for main process to distribute data.')
            dataset = load_from_disk(features_dir)
            table = dataset.data
            start = 0
            for node in range(self.num_nodes):
                sub_length = math.ceil(len(table)/self.num_nodes)
                rank_table = table.slice(offset=start, length=sub_length)
                start += sub_length
                rank_dataset = arrow_dataset.Dataset(
                    arrow_table=rank_table,
                    info=dataset.info,
                    split=dataset.split,
                    fingerprint=dataset._fingerprint,
                )
                new_features_dir = os.path.join(features_dir, f'node_{node}')
                rank_dataset.save_to_disk(new_features_dir)
        torch.distributed.barrier()
        logging.info('Finished waiting for main process in distribute_dataset().')

    def get_dataset(self, task):
        features_dir = os.path.join(self.dir_path, self.split, f'features_{task.task_id}')
        app_state = AppState()
        #rank = app_state.global_rank
        rank = parallel_state.get_data_parallel_rank()
        #world_size = app_state.world_size
        world_size = parallel_state.get_data_parallel_world_size()
        node = rank // self.num_gpus
        if not os.path.isdir(features_dir) or not self.use_cache:
            self.map_dataset(task, rank, features_dir)
        if self.distribute_datasets and self.num_nodes > 1:
            self.distribute_dataset(rank, world_size, features_dir)
            features_dir = os.path.join(features_dir, f'node_{node}')
        logging.info('Loading results from the main process %s.' % features_dir)
        dataset = load_from_disk(features_dir, keep_in_memory=True)
        dataset.task = task
        return dataset

    def get_task(self, file_path, dt_name, subset):
        task = Task(file_path, dt_name, subset)
        task.create_example = self.create_example
        task.tokenize = self.tokenize
        self.tasks.append(task)
        return task

    def get_data_dict(self):
        if self.split == 'train':
            data_dict = DATA_ORG[self.t0_type]
        else:
            data_dict = t0_all_evaldt_names_subset
        dataset_dict = {}
        for dt_name in data_dict.keys():
            logging.info('Dataset name %s.' % dt_name)
            subsets = data_dict[dt_name]
            if not isinstance(subsets, list):
                subsets = [subsets]
            for subset in subsets:
                logging.info('Subset name %s.' % subset)
                if "/" in dt_name:
                    dt_name = dt_name.split("/")[-1]
                file_name = "_%s_%s.jsonl" % (dt_name, "" if subset is None else subset)
                _, data_paths = get_data_paths_and_splits(self.split, self.dir_path, file_name, dt_name)
                for file_path in data_paths:
                    task = self.get_task(file_path, dt_name, subset)
                    task_name = "%s_%s" % (dt_name, "" if subset is None else subset)
                    dataset_dict[task_name] = self.get_dataset(task)
        return dataset_dict

    def get_sampling_probs(self):
        sampling_data_sizes = []
        for dataset in self.datasets.values():
            max_sampling_size = self.max_sampling_size//(self.num_nodes if self.distribute_datasets else 1)
            sampling_data_sizes.append(min(len(dataset), max_sampling_size))
        sampling_data_sizes = np.array(sampling_data_sizes)
        sampling_probs = sampling_data_sizes / np.sum(sampling_data_sizes)
        return sampling_probs.tolist()

    def __len__(self):
        return sum(len(dataset) for dataset in self.datasets.values())

    def choose_template(self, features):
        available_prompts = []
        for data_name in features.keys():
            if data_name.startswith("text_enc") and (
                features[data_name] is not None and
                not np.any(np.array(features[data_name]) == self.empty_prompt_token_id)
            ):
                available_prompts.append(data_name.split("_")[-1])
        assert available_prompts
        prompt_num = np.random.choice(available_prompts)
        chosen_features = {
            'text_enc': features[f'text_enc_{prompt_num}'],
            'text_dec': features[f'text_dec_{prompt_num}'],
            'labels': features[f'labels_{prompt_num}'],
            'task_id': features[f'task_id_{prompt_num}'],
            'prompt_id': features[f'prompt_id_{prompt_num}']
        }
        return chosen_features

    @staticmethod
    def create_example(data, task_id, prompt_id):
        return InputPromptedExample(
            task_id=task_id,
            text=data['input'],
            prompt_id=prompt_id,
            label=data['output']
        )

    def collate_fn(self, batch):
        new_batch = []
        for features in batch:
            feature = self.choose_template(features)
            new_batch.append(feature)
        return self.collate_fn2(new_batch)

    def tokenize(self, example):
        if example.input_text is None:
            enc_query = [self.empty_prompt_token_id]
            dec_input = [self.empty_prompt_token_id]
            labels = [self.empty_prompt_token_id]
        else:
            enc_query = self.tokenizer.text_to_ids(example.input_text)
            if len(enc_query) > self.max_query_length:
                enc_query = enc_query[: self.max_query_length]
            dec_query = (
                    [self.tokenizer.cls_id]
                    + self.tokenizer.text_to_ids(example.label)
                    + [self.tokenizer.eos_id]
            )
            if len(dec_query) > self.max_query_length_decoder + 1:
                dec_query = dec_query[: self.max_query_length_decoder + 1]
            dec_input = dec_query[:-1]
            labels = dec_query[1:]
        task_id = [example.task_id]
        prompt_id = [example.prompt_id]
        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'labels': labels,
            'task_id': task_id,
            'prompt_id': prompt_id
        }

    def collate_fn2(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]
        task_ids = [item['task_id'] for item in batch]
        prompt_ids = [item['prompt_id'] for item in batch]

        max_dec_input_length = max([len(item) for item in dec_input])
        max_enc_query_length = max([len(item) for item in enc_query])
        max_label_length = max([len(item) for item in labels])

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        task_ids = torch.LongTensor(task_ids)
        prompt_ids = torch.LongTensor(prompt_ids)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = (enc_query != self.tokenizer.pad_id).long()
        dec_mask = (dec_input != self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query[:, :self.max_query_length],
            'text_dec': dec_input[:, :self.max_query_length_decoder],
            'labels': labels[:, :self.max_query_length_decoder],
            'loss_mask': loss_mask[:, :self.max_query_length_decoder],
            'enc_mask': enc_mask[:, :self.max_query_length],
            'dec_mask': dec_mask[:, :self.max_query_length_decoder],
            'task_ids': task_ids,
            'prompt_ids': prompt_ids
        }


class T0PrimeHFDatasetBuilder(T0HFDatasetBuilder):
    """T0' Dataset Builder in a text-to-text format."""

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return

    def __init__(
            self,
            t0_type: str,
            dir_path: str,
            max_sampling_size: int,
            split: str,
            tokenizer: TokenizerSpec,
            max_seq_length: int,
            prompt_token_id: int,
            prompt_seq_len: int,
            max_seq_length_decoder: int = 128,
            seed: int = 43,
            use_cache: bool = True,
            distribute_datasets: bool = True,
            extension: str = 'json',
            max_samples: int = None,
            num_proc: int = None,
            num_gpus: int = None,
            num_nodes: int = None,
            split_template: bool = True,
    ):
        """
        Processes T0' dataset using differentiable prompts
        Args:
            t0_type: [train_t0, train_t0p, train_t0pp, t0_debug]
            split: train, test, eval
            max_sampling_size: caps probabilities at max size for a specific dataset
            dir_path: path to data directory for train, evaluation or test
            tokenizer: such as AutoTokenizer
            max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
            max_seq_length_decoder: max sequence length
            use_cache: whether to use data cache
            max_samples: limit size of dataset (not implemented)
            prompt_token_id: tokenizer id for [PROMPT] token
            prompt_seq_len: sequence lenght of prompt
            split_template: whether to seperate template tokens
        """
        self.prompt_token_id = prompt_token_id
        self.prompt_seq_len = prompt_seq_len
        self.split_template = split_template
        super().__init__(
            t0_type, dir_path, max_sampling_size, split, tokenizer, max_seq_length, max_seq_length_decoder,
            seed, use_cache, distribute_datasets, extension, max_samples, num_proc, num_gpus, num_nodes
        )

    @staticmethod
    def create_example(data, task_id, prompt_id):
        return InputPromptedExample(
            task_id=task_id,
            text=data['input'],
            prompt_id=prompt_id,
            label=data['output'],
            chunked_idx=data['chunked_idx']
        )

    def tokenize(self, example):
        def get_text_chunks(input_text, chunked_idx):
            """
            Splits in the input text into chunks such that:
            chunked(input_text) = [[orig_txt_part1], [template_part1], [orig_txt_part2], [template_part2], ...]
            or other interleaved patterns of template and original text.
            Assumes already sorted lists of index ranges.
            """
            text_chunks = []
            for chunk in chunked_idx.split(","):
                chunk_name, chunk_start, chunk_end = chunk.split("-")
                text_chunks.append((chunk_name, input_text[int(chunk_start):int(chunk_end)]))
            return text_chunks

        input_text_chunks = get_text_chunks(example.input_text, example.chunked_idx)
        enc_query = []
        template = []
        for chunk in input_text_chunks:
            chunk_name = chunk[0]
            chunk_tokens = self.tokenizer.text_to_ids(chunk[1])
            if chunk_name == TEMPLATE_CHUNK_NAME and self.split_template:
                remain = max(0, self.prompt_seq_len - len(template) - len(chunk_tokens))
                template.extend(chunk_tokens[:remain])
                enc_query.extend([self.prompt_token_id] * len(chunk_tokens[:remain]))
            else:
                max_length = self.max_query_length + (0 if self.split_template else self.prompt_seq_len)
                remain = max(0, max_length - len(enc_query) - len(chunk_tokens))
                enc_query.extend(chunk_tokens[:remain])  # only reduce original chunk
        dec_query = (
                [self.tokenizer.cls_id]
                + self.tokenizer.text_to_ids(example.label)
                + [self.tokenizer.eos_id]
        )
        if len(dec_query) > self.max_query_length_decoder + 1:
            dec_query = dec_query[: self.max_query_length_decoder + 1]
        dec_input = dec_query[:-1]
        labels = dec_query[1:]
        task_id = [example.task_id]
        prompt_id = [example.prompt_id]
        return {
            'text_enc': enc_query,
            'template': template,
            'text_dec': dec_input,
            'labels': labels,
            'task_id': task_id,
            'prompt_id': prompt_id
        }

    def collate_fn2(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        template = [item['template'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]
        task_ids = [item['task_id'] for item in batch]
        prompt_ids = [item['prompt_id'] for item in batch]
        
        if self.split_template:
            max_template_length = max(self.prompt_seq_len, max([len(item) for item in template]))
            enc_query = [item_q + [self.prompt_token_id] * (max_template_length - len(item_t)) for item_q, item_t in zip(enc_query, template)]

        max_dec_input_length = max([len(item) for item in dec_input])
        max_enc_query_length = max([len(item) for item in enc_query])
        max_label_length = max([len(item) for item in labels])

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        template = [item[:self.prompt_seq_len] + [self.tokenizer.pad_id] * (self.prompt_seq_len - len(item)) for item in template]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        template = torch.LongTensor(template)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        task_ids = torch.LongTensor(task_ids)
        prompt_ids = torch.LongTensor(prompt_ids)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = (enc_query != self.tokenizer.pad_id).long()
        dec_mask = (dec_input != self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'template': template,
            'labels': labels,
            'loss_mask': loss_mask,
            'enc_mask': enc_mask,
            'dec_mask': dec_mask,
            'task_ids': task_ids,
            'prompt_ids': prompt_ids
        }


class TaskDataset(Dataset):

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return

    def __init__(
            self,
            task: Task,
            features: List[Dict[str, List[int]]],
            empty_prompt_token_id: int,
    ):
        self.task = task
        self.features = features
        self.empty_prompt_token_id = empty_prompt_token_id

    def __len__(self):
        return len(self.features)

    def __getitem__(self, idx):
        feature = self.features[idx]
        return self.choose_template(feature)

    def get_chosen_features(self, feature_dict, prompt_num):
        raise "Dataset `get_chosen_features` not implemented"

    def choose_template(self, feature_dict):
        available_prompts = []
        for data_name in feature_dict.keys():
            if data_name.startswith("text_enc") and (
                feature_dict[data_name] is not None and
                not np.any(np.array(feature_dict[data_name]) == self.empty_prompt_token_id)
            ):
                available_prompts.append(data_name.split("_")[-1])
        assert available_prompts
        prompt_num = np.random.choice(available_prompts)
        return self.get_chosen_features(feature_dict, prompt_num)


class T0DatasetBuilder(object):
    """T0 Dataset Builder in a text-to-text format."""

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return

    def __init__(
            self,
            t0_type: str,
            dir_path: str,
            max_sampling_size: int,
            split: str,
            tokenizer: TokenizerSpec,
            max_seq_length: int,
            max_seq_length_decoder: int = 128,
            seed: int = 43,
            use_cache: bool = True,
            extension: str = 'json',
            max_samples: int = None,
            num_proc: int = None,
            num_gpus: int = None,
            num_nodes: int = 1,
            num_data_shards: int = 1
    ):
        """
        Processes T0 dataset
        Args:
            t0_type: [train_t0, train_t0p, train_t0pp, t0_debug]
            split: train, test, eval
            max_sampling_size: caps probabilities at max size for a specific dataset
            dir_path: path to data directory for train, evaluation or test
            tokenizer: such as AutoTokenizer
            max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
            max_seq_length_decoder: max sequence length
            buffer_size: size of the buffer, chunks of data to suffle
            use_cache: whether to use data cache
            max_samples: limit size of dataset (not implemented)
            num_proc: number of processes to load data
        """
        self.t0_type = t0_type
        self.dir_path = dir_path
        self.max_sampling_size = max_sampling_size
        self.split = split
        self.tokenizer = tokenizer
        self.max_query_length = max_seq_length
        self.max_query_length_decoder = max_seq_length_decoder
        self.seed = seed
        self.use_cache = use_cache
        self.extension = extension
        self.max_samples = max_samples
        self.num_proc = num_proc if num_proc > 0 else None
        self.num_gpus = num_gpus
        self.num_nodes = num_nodes
        self.num_data_shards = num_data_shards if split == 'train' else 1
        self.tasks = []
        self.empty_prompt_token_id = -1
        self.set_data_dict()

    def set_data_dict(self, shard=0):
        if self.split == 'train':
            data_dict = DATA_ORG[self.t0_type]
        else:
            data_dict = t0_all_evaldt_names_subset
        dataset_dict = {}
        for dt_name in data_dict.keys():
            logging.info('Dataset name %s.' % dt_name)
            subsets = data_dict[dt_name]
            if not isinstance(subsets, list):
                subsets = [subsets]
            for subset in subsets:
                logging.info('Subset name %s.' % subset)
                if "/" in dt_name:
                    dt_name = dt_name.split("/")[-1]
                file_name = "_%s_%s.jsonl" % (dt_name, "" if subset is None else subset)
                _, data_paths = get_data_paths_and_splits(self.split, self.dir_path, file_name, dt_name)
                for file_path in data_paths:
                    task = self.get_task(file_path, dt_name, subset)
                    task_name = "%s_%s" % (dt_name, "" if subset is None else subset)
                    dataset_dict[task_name] = self.get_dataset(task, shard)
        self.datasets = dataset_dict

    def get_task(self, file_path, dt_name, subset):
        task = Task(file_path, dt_name, subset)
        task.create_example = self.create_example
        task.tokenize = self.tokenize
        self.tasks.append(task)
        return task

    def get_dataset(self, task, shard):
        features_dir_name = f'cache_{task.dt_name}'
        if task.subset is not None:
            features_dir_name += f'_{task.subset}'
        if getattr(self, 'split_template', False):
            features_dir_name += '_seperated'
        features_dir = os.path.join(self.dir_path, self.split, features_dir_name)
        cached_features_files = [os.path.join(
            features_dir, f"cached_{self.tokenizer.name}_{self.max_query_length}_{shard}"
        ) for shard in range(self.num_data_shards)]
        rank = parallel_state.get_data_parallel_rank()
        if os.path.isdir(features_dir):
            cache_exists = all(os.path.exists(cache) for cache in cached_features_files)
        else:
            if rank == 0:
                os.mkdir(features_dir)
            cache_exists = False
        if not self.use_cache or not cache_exists:
            self.map_features(task, rank, cached_features_files)
        logging.info(f"loading from {cached_features_files[shard]}")
        with open(cached_features_files[shard], "rb") as reader:
            features = pickle.load(reader)
        dataset = TaskDataset(task, features, self.empty_prompt_token_id)
        dataset.get_chosen_features = self.get_chosen_features
        return dataset

    def map_features(self, task, rank, cached_features_files):
        logging.info('Waiting for main process to perform the feature processing.')
        if rank == 0:
            dataset = load_dataset(
                self.extension, data_files=task.file_path, split='train'
            )
            if self.max_samples is not None:
                dataset = dataset.select(range(min(len(dataset), self.max_samples)))
            original_column_names = dataset.column_names
            dataset = dataset.map(
                function=task.map_fn_train if self.split == 'train' else task.map_fn_eval,
                num_proc=self.num_proc,
                remove_columns=original_column_names,
                load_from_cache_file=False
            )
            self.shard_save_features(dataset, cached_features_files)
        torch.distributed.barrier()
        logging.info('Finished waiting for main process in map_dataset().')

    def shard_save_features(self, dataset, cached_features_files):
        logging.info('Waiting for main process to shard and save features.')
        num_examples = len(dataset)
        features = dataset.to_dict()
        shard_size = num_examples // self.num_data_shards
        features = [{k: v[i] for k, v in features.items()} for i in range(num_examples)]
        features = [features[i:i + shard_size] for i in range(0, num_examples, shard_size)]
        for features_shard, file_name in zip(features, cached_features_files):
            with open(file_name, "wb") as writer:
                pickle.dump(features_shard, writer)

    def assemble_datasets(self):
        if self.split == 'train':
            datasets_list = list(self.datasets.values())
            datasets = BlendableDataset(
                datasets=datasets_list,
                weights=self.get_sampling_probs()
            )
            return datasets
        else:
            return self.datasets

    def get_sampling_probs(self):
        sampling_data_sizes = []
        for dataset in self.datasets.values():
            max_sampling_size = self.max_sampling_size//self.num_data_shards
            sampling_data_sizes.append(min(len(dataset), max_sampling_size))
        sampling_data_sizes = np.array(sampling_data_sizes)
        sampling_probs = sampling_data_sizes / np.sum(sampling_data_sizes)
        return sampling_probs.tolist()

    def __len__(self):
        return sum(len(dataset) for dataset in self.datasets.values())

    @staticmethod
    def get_chosen_features(feature_dict, prompt_num):
        return {
            'text_enc': feature_dict[f'text_enc_{prompt_num}'],
            'text_dec': feature_dict[f'text_dec_{prompt_num}'],
            'labels': feature_dict[f'labels_{prompt_num}'],
            'task_id': feature_dict[f'task_id_{prompt_num}'],
            'prompt_id': feature_dict[f'prompt_id_{prompt_num}']
        }

    @staticmethod
    def create_example(data, task_id, prompt_id):
        return InputPromptedExample(
            task_id=task_id,
            text=data['input'],
            prompt_id=prompt_id,
            label=data['output']
        )

    def tokenize(self, example):
        if example.input_text is None:
            enc_query = [self.empty_prompt_token_id]
            dec_input = [self.empty_prompt_token_id]
            labels = [self.empty_prompt_token_id]
        else:
            enc_query = self.tokenizer.text_to_ids(example.input_text)
            if len(enc_query) > self.max_query_length:
                enc_query = enc_query[: self.max_query_length]
            dec_query = (
                    [self.tokenizer.cls_id]
                    + self.tokenizer.text_to_ids(example.label)
                    + [self.tokenizer.eos_id]
            )
            if len(dec_query) > self.max_query_length_decoder + 1:
                dec_query = dec_query[: self.max_query_length_decoder + 1]
            dec_input = dec_query[:-1]
            labels = dec_query[1:]
        task_id = [example.task_id]
        prompt_id = [example.prompt_id]
        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'labels': labels,
            'task_id': task_id,
            'prompt_id': prompt_id
        }

    def collate_fn(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]
        task_ids = [item['task_id'] for item in batch]
        prompt_ids = [item['prompt_id'] for item in batch]

        max_dec_input_length = max([len(item) for item in dec_input])
        max_enc_query_length = max([len(item) for item in enc_query])
        max_label_length = max([len(item) for item in labels])

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        task_ids = torch.LongTensor(task_ids)
        prompt_ids = torch.LongTensor(prompt_ids)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = (enc_query != self.tokenizer.pad_id).long()
        dec_mask = (dec_input != self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query[:, :self.max_query_length],
            'text_dec': dec_input[:, :self.max_query_length_decoder],
            'labels': labels[:, :self.max_query_length_decoder],
            'loss_mask': loss_mask[:, :self.max_query_length_decoder],
            'enc_mask': enc_mask[:, :self.max_query_length],
            'dec_mask': dec_mask[:, :self.max_query_length_decoder],
            'task_ids': task_ids,
            'prompt_ids': prompt_ids
        }


class T0PrimeDatasetBuilder(T0DatasetBuilder):
    """T0' Dataset Builder in a text-to-text format."""

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return

    def __init__(
            self,
            t0_type: str,
            dir_path: str,
            max_sampling_size: int,
            split: str,
            tokenizer: TokenizerSpec,
            max_seq_length: int,
            prompt_token_id: int,
            prompt_seq_len: int,
            max_seq_length_decoder: int = 128,
            seed: int = 43,
            use_cache: bool = True,
            extension: str = 'json',
            max_samples: int = None,
            num_proc: int = None,
            num_gpus: int = None,
            num_nodes: int = None,
            num_data_shards: int = 1,
            split_template: bool = True,
    ):
        """
        Processes T0' dataset using differentiable prompts
        Args:
            t0_type: [train_t0, train_t0p, train_t0pp, t0_debug]
            split: train, test, eval
            max_sampling_size: caps probabilities at max size for a specific dataset
            dir_path: path to data directory for train, evaluation or test
            tokenizer: such as AutoTokenizer
            max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
            max_seq_length_decoder: max sequence length
            use_cache: whether to use data cache
            max_samples: limit size of dataset (not implemented)
            prompt_token_id: tokenizer id for [PROMPT] token
            prompt_seq_len: sequence lenght of prompt
            split_template: whether to seperate template tokens
        """
        self.prompt_token_id = prompt_token_id
        self.prompt_seq_len = prompt_seq_len if split_template else 1
        self.split_template = split_template
        super().__init__(
            t0_type, dir_path, max_sampling_size, split, tokenizer,
            max_seq_length, max_seq_length_decoder, seed, use_cache,
            extension, max_samples, num_proc, num_gpus, num_nodes, num_data_shards
        )

    @staticmethod
    def get_chosen_features(feature_dict, prompt_num):
        return {
            'text_enc': feature_dict[f'text_enc_{prompt_num}'],
            'template': feature_dict[f'template_{prompt_num}'],
            'text_dec': feature_dict[f'text_dec_{prompt_num}'],
            'labels': feature_dict[f'labels_{prompt_num}'],
            'task_id': feature_dict[f'task_id_{prompt_num}'],
            'prompt_id': feature_dict[f'prompt_id_{prompt_num}']
        }

    @staticmethod
    def create_example(data, task_id, prompt_id):
        return InputPromptedExample(
            task_id=task_id,
            text=data['input'],
            prompt_id=prompt_id,
            label=data['output'],
            chunked_idx=data['chunked_idx']
        )

    def tokenize(self, example):
        def get_text_chunks(input_text, chunked_idx):
            """
            Splits in the input text into chunks such that:
            chunked(input_text) = [[orig_txt_part1], [template_part1], [orig_txt_part2], [template_part2], ...]
            or other interleaved patterns of template and original text.
            Assumes already sorted lists of index ranges.
            """
            text_chunks = []
            for chunk in chunked_idx.split(","):
                chunk_name, chunk_start, chunk_end = chunk.split("-")
                text_chunks.append((chunk_name, input_text[int(chunk_start):int(chunk_end)]))
            return text_chunks
        if example.input_text is None:
            enc_query = [self.empty_prompt_token_id]
            template = [self.empty_prompt_token_id]
            dec_input = [self.empty_prompt_token_id]
            labels = [self.empty_prompt_token_id]
        else:
            input_text_chunks = get_text_chunks(example.input_text, example.chunked_idx)
            enc_query = []
            template = []
            for chunk in input_text_chunks:
                chunk_name = chunk[0]
                chunk_tokens = self.tokenizer.text_to_ids(chunk[1])
                if chunk_name == TEMPLATE_CHUNK_NAME and self.split_template:
                    remain = max(0, self.prompt_seq_len - len(template) - len(chunk_tokens))
                    template.extend(chunk_tokens[:remain])
                    enc_query.extend([self.prompt_token_id] * len(chunk_tokens[:remain]))
                else:
                    max_length = self.max_query_length + (0 if self.split_template else self.prompt_seq_len)
                    remain = max(0, max_length - len(enc_query) - len(chunk_tokens))
                    enc_query.extend(chunk_tokens[:remain])  # only reduce original chunk
            dec_query = (
                    [self.tokenizer.cls_id]
                    + self.tokenizer.text_to_ids(example.label)
                    + [self.tokenizer.eos_id]
            )
            if len(dec_query) > self.max_query_length_decoder + 1:
                dec_query = dec_query[: self.max_query_length_decoder + 1]
            dec_input = dec_query[:-1]
            labels = dec_query[1:]
            if not template:
                template = [self.tokenizer.pad_id]
        task_id = [example.task_id]
        prompt_id = [example.prompt_id]
        return {
            'text_enc': enc_query,
            'template': template,
            'text_dec': dec_input,
            'labels': labels,
            'task_id': task_id,
            'prompt_id': prompt_id
        }

    def collate_fn(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        template = [item['template'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]
        task_ids = [item['task_id'] for item in batch]
        prompt_ids = [item['prompt_id'] for item in batch]

        if self.split_template:
            max_template_length = max(self.prompt_seq_len, max([len(item) for item in template]))
            enc_query = [item_q + [self.prompt_token_id] * (max_template_length - len(item_t)) for item_q, item_t in
                         zip(enc_query, template)]

        max_dec_input_length = max([len(item) for item in dec_input])
        max_enc_query_length = max([len(item) for item in enc_query])
        max_label_length = max([len(item) for item in labels])

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        template = [item[:self.prompt_seq_len] + [self.tokenizer.pad_id] * (self.prompt_seq_len - len(item)) for item in
                    template]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        template = torch.LongTensor(template)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        task_ids = torch.LongTensor(task_ids)
        prompt_ids = torch.LongTensor(prompt_ids)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = (enc_query != self.tokenizer.pad_id).long()
        dec_mask = (dec_input != self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query[:, :self.max_query_length],
            'text_dec': dec_input[:, :self.max_query_length_decoder],
            'template': template[:, :self.prompt_seq_len],
            'labels': labels[:, :self.max_query_length_decoder],
            'loss_mask': loss_mask[:, :self.max_query_length_decoder],
            'enc_mask': enc_mask[:, :self.max_query_length],
            'dec_mask': dec_mask[:, :self.max_query_length_decoder],
            'task_ids': task_ids,
            'prompt_ids': prompt_ids
        }