# -*- coding: utf-8 -*-
import json
import logging
import os
import shutil

import torch


class ModelAbstract(torch.nn.Module):
    """
    To be used for all sub-models (ex, layers in a main model).
    """
    def __init__(self):
        super().__init__()
        self.log = logging.getLogger()  # Gets the root

    @property
    def params(self):
        """All parameters necessary to create again the same model"""
        return {}

    def set_log(self, log: logging.Logger):
        """Possibility to pass a tqdm-compatible logger in case the dataloader
        is iterated through a tqdm progress bar. Note that, of course, log
        outputs will be confusing, particularly in debug mode, considering
        that the dataloader may use more than one method in parallel."""
        self.log = log


class MainModelAbstract(ModelAbstract):
    """
    To be used for all models that will be trained. Defines the way to save
    the model.

    It should also define a forward() method.
    """
    def __init__(self, experiment_name='my_model'):
        super().__init__()
        self.experiment_name = experiment_name
        self.best_model_state = None

    @property
    def params(self):
        """All parameters necessary to create again the same model. Will be
        used in the trainer, when saving the checkpoint state. Params here
        will be used to re-create the model when starting an experiment from
        checkpoint. You should be able to re-create an instance of your
        model with those params."""
        return {}

    @classmethod
    def init_from_checkpoint(cls, **params):
        model = cls(**params)
        return model

    def update_best_model(self):
        # Initialize best model
        # Uses torch's module state_dict.
        self.best_model_state = self.state_dict()

    def save(self, saving_dir):
        # Make model directory
        model_dir = os.path.join(saving_dir, "model")

        # If a model was already saved, back it up and erase it after saving
        # the new.
        to_remove = None
        if os.path.exists(model_dir):
            to_remove = os.path.join(saving_dir, "model_old")
            shutil.move(model_dir, to_remove)
        os.makedirs(model_dir)

        # Save attributes
        name = os.path.join(model_dir, "parametersr.json")
        with open(name, 'w') as json_file:
            json_file.write(json.dumps(self.params, indent=4,
                                       separators=(',', ': ')))

        # Save model
        torch.save(self.best_model_state,
                   os.path.join(model_dir, "best_model_state.pkl"))

        if to_remove:
            shutil.rmtree(to_remove)

    def compute_loss(self, outputs, targets):
        raise NotImplementedError
