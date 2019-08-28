from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
from torch.utils.data import Sampler


class AbstractBatchSampler(Sampler):
    """
    Samplers that inherit from this class should work out of the box.
    Just define the __iter__ function
    __init__ defines self._data_size and self._batch_size
    as well as self._random RNG if needed
    """
    def __init__(self, data_size, batch_size, seed=0):
        self._data_size = int(data_size)
        if self._data_size < 0:
            raise ValueError('%s received negative data size %s', (self.__class__.__name__, str(data_size)))

        self._batch_size = int(batch_size)
        if self._batch_size < 0 or self._batch_size > self._data_size:
            raise ValueError('%s received invalid batch size %d for data size %s', (self.__class__.__name__, batch_size, str(self._data_size)))
        # Use an independent random number generator for random sampling
        if seed<0:
            import time
            seed=int(time.time())
        self._random = np.random.RandomState(seed=seed)

    def __len__(self):
        return self._data_size


class RandomSequenceSampler(AbstractBatchSampler):
    def __iter__(self):
        starts = self._random.randint(low=0, high=self._data_size - self._batch_size, size=(len(self),))
        return iter(np.concatenate([np.arange(start, start+self._batch_size) for start in starts]))

    @staticmethod
    def create(ds, cfg):
        return RandomSequenceSampler(len(ds), cfg['batch_size'], seed=cfg.get('seed',-1))


class SequentialBatchSampler(AbstractBatchSampler):
    def __iter__(self):
        starts = np.arange(0, self._data_size - self._batch_size, self._batch_size)
        return iter(np.concatenate([np.arange(start, start+self._batch_size) for start in starts]))

    @staticmethod
    def create(ds, cfg):
        return SequentialBatchSampler(len(ds), cfg['batch_size'])
