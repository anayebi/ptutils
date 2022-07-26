import os
import shutil
import torch
import torch.nn as nn
import ptutils
from ptutils.model_training.dbinterface import MongoInterface
from ptutils.model_training.train_utils import parse_config
from ptutils.core.default_constants import USE_MONGODB
from ptutils.core.utils import set_seed


class Trainer:
    def __init__(self, config):
        if isinstance(config, str):
            # Read configuration file first if filepath
            self.config = parse_config(config)
        else:
            assert isinstance(config, dict)
            self.config = config

        # Set up checkpoint save directory
        self.save_dir = self._make_save_dir()

        # Set reproducibility seed
        self._set_seed()

        # Set device, print function, model, loss, etc.
        self.device = self._set_device()
        self.model, self.model_name = self.initialize_model()
        self.train_loader, self.val_loader = self.initialize_dataloader()
        self.loss_func = self.initialize_loss_function()
        self.optimizer = self.initialize_optimizer()
        self.initialize_scheduler()

        # Set MongoDB Interface if used
        self.database = None
        cfg_save_keys = ["db_name", "coll_name", "exp_id"]
        assert set(cfg_save_keys).issubset(set(list(self.config.keys())))
        for k in cfg_save_keys:
            assert self.config[k] is not None

        if self.config.get("use_mongodb", USE_MONGODB):
            assert "port" in self.config.keys()
            assert self.config["port"] is not None
            self.database = MongoInterface(
                database_name=self.config["db_name"],
                collection_name=self.config["coll_name"],
                port=self.config["port"],
                print_fn=self.print_fn,
            )

        # This will be changed depending on whether or not we are loading from a
        # checkpoint. See the derived class' implementation of load_checkpoint().
        self.current_epoch = 0

        # Before doing anything else, save the configuration file to exp directory
        # so we can remember the exact experiment settings.
        self._save_config_file(self.config["filepath"])

        # If resume_checkpoint is provided, then load from checkpoint
        self.check_key("resume_checkpoint")
        if self.config["resume_checkpoint"] is not None:
            if self.use_tpu:
                from ptutils.model_training.gcloud_utils import (
                    download_file_from_bucket,
                )

                # Download from gs bucket to local directory, where each ordinal has
                # its own copy of the same file so that they are each loading the same
                # data
                self.config["resume_checkpoint"] = download_file_from_bucket(
                    filename=self.config["resume_checkpoint"],
                    ordinal=self.rank,
                    print_fn=self.print_fn,
                )
            self.load_checkpoint()
            if self.use_tpu:
                # Remove the file locally after loading from gs bucket
                os.remove(self.config["resume_checkpoint"])

    def check_key(self, key):
        assert hasattr(self, "config")
        assert key in self.config.keys(), f"{key} undefined in config file."

    def _make_save_dir(self):
        self.check_key("save_dir")
        save_dir = self.config["save_dir"]
        if not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)

        return save_dir

    def _save_config_file(self, config_file_path):
        assert hasattr(self, "use_tpu")
        assert hasattr(self, "save_dir")

        config_filename = config_file_path.split("/")[-1]
        config_copy_name = os.path.join(self.save_dir, f"{config_filename}")
        if self.rank == 0:
            shutil.copyfile(config_file_path, config_copy_name)
            if self.use_tpu:
                from ptutils.model_training.gcloud_utils import save_file_to_bucket

                save_file_to_bucket(filename=config_copy_name)

    def _save_to_db(self, curr_state, save_keys):
        # NOTE: these keys purposefully exclude the state dicts because the state
        # dicts have NOT been coordinated across tpu cores yet, that is only done
        # by the xm.save() cmd in save_checkpoint. These included keys to be saved
        # to the database, however, are the same across tpu cores since they are
        # the result of xm.mesh_reduce().
        record = {"exp_id": self.config["exp_id"]}
        record.update({k: curr_state[k] for k in save_keys})
        if (self.rank == 0) and (self.database is not None):
            self.database.save(record)

    def _set_seed(self):
        """
        Sets the random seed to make entire training process reproducible.

        Inputs:
            seed : (int) random seed
        """
        self.check_key("seed")
        seed = self.config["seed"]

        set_seed(seed)

    def _set_device(self):
        self.check_key("gpus")
        self.check_key("tpu")

        self.print_fn = print
        self.use_tpu = False

        if self.config["tpu"]:
            # TPU device; Use TPU
            assert not self.config["gpus"], f"Cannot enable both TPU and GPU."

            import torch_xla.core.xla_model as xm

            device = xm.xla_device()
            self.use_tpu = True
            self.rank = xm.get_ordinal()
            self.world_size = xm.xrt_world_size()
            self.print_fn = xm.master_print
            self.print_fn(f"Using TPU...")

        elif self.config["gpus"]:
            # GPU device; Use GPU
            assert torch.cuda.is_available(), "Cannot use GPU."
            assert not self.config["tpu"], f"Cannot enable both TPU and GPU."

            import torch.distributed as dist

            # each subprocess gets its own gpu
            self.gpu_ids = self.config["gpus"]
            assert len(self.gpu_ids) == 1
            self.rank = dist.get_rank()
            self.world_size = dist.get_world_size()
            torch.cuda.set_device(self.gpu_ids[0])
            device = torch.device(f"cuda:{self.gpu_ids[0]}")
            self.print_fn(
                f"Subprocess {self.rank} is on GPU {self.gpu_ids}. {self.world_size} GPUs total."
            )

        else:
            # CPU not supported, makes code ugly
            # and we likely won't test this use case anyway
            raise ValueError

        return device

    def adjust_learning_rate(self):
        pass

    def initialize_scheduler(self):
        pass

    def get_model(self, model_name):
        """
        Inputs:
            model_name : (string) Name of deep net architecture.

        Outputs:
            model     : (torch.nn.DataParallel) model
        """
        model = ptutils.models.__dict__[model_name]()
        return model

    def initialize_model(self):
        assert hasattr(self, "device")
        assert hasattr(self, "use_tpu")
        self.check_key("model")

        model = self.get_model(self.config["model"],)

        model = model.to(self.device)

        # gpu training
        if not self.use_tpu:
            assert hasattr(self, "gpu_ids")
            model = nn.parallel.DistributedDataParallel(model, device_ids=self.gpu_ids)

        model_name = self.config["model"]

        return model, model_name

    def train(self):
        """
        Main entry point for training a model.
        """
        assert hasattr(self, "train_loader")
        self.check_key("save_freq")
        self.check_key("num_epochs")

        for i in range(self.current_epoch, self.config["num_epochs"]):
            self.current_epoch = i
            # See warning in:
            # https://pytorch.org/docs/stable/data.html#torch.utils.data.distributed.DistributedSampler
            if self.use_tpu:
                self.train_loader._loader.sampler.set_epoch(i)
            else:
                self.train_loader.sampler.set_epoch(i)

            self.adjust_learning_rate()
            self.train_one_epoch()
            self.validate()
            self.save_checkpoint()

        self.close_db()

    def set_model_to_train(self):
        self.model.train()
        assert self.model.training

    def set_model_to_eval(self):
        self.model.eval()
        assert not self.model.training

    def close_db(self):
        if (self.rank == 0) and (self.database is not None):
            self.database.sync_with_host()

    def initialize_loss_function(self):
        """
        This function should return an instance of the loss function class.
        """
        raise NotImplementedError

    def initialize_dataloader(self):
        """
        This function should return a length-two tuple of PyTorch dataloaders,
        where the first loader is for the training set and the second loader
        is for the validation set.
        """
        raise NotImplementedError

    def initialize_optimizer(self):
        """
        This function should return a PyTorch optimizer object.
        """
        raise NotImplementedError

    def train_one_epoch(self):
        raise NotImplementedError

    def validate(self):
        raise NotImplementedError

    def save_checkpoint(self):
        raise NotImplementedError

    def load_checkpoint(self):
        raise NotImplementedError
