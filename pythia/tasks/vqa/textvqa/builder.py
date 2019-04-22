# Copyright (c) Facebook, Inc. and its affiliates.
from pythia.tasks.vqa.vizwiz import VizWizBuilder
from pythia.common.registry import Registry
from .dataset import TextVQADataset


@Registry.register_builder('textvqa')
class TextVQABuilder(VizWizBuilder):
    def __init__(self):
        super().__init__()
        self.dataset_name = 'textvqa'
        self.set_dataset_class(TextVQADataset)