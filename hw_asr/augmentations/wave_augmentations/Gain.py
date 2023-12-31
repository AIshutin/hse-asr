import torch_audiomentations
from torch import Tensor

from hw_asr.augmentations.base import AugmentationBatchBased


class Gain(AugmentationBatchBased):
    def __init__(self, *args, **kwargs):
        super().__init__(torch_audiomentations.Gain(*args, **kwargs))
