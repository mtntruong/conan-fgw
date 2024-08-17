import logging
import random
import string
from argparse import Namespace
import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import (
    EarlyStopping,
    ModelCheckpoint,
    GradientAccumulationScheduler,
    LearningRateMonitor,
    Timer,
    StochasticWeightAveraging,
)
from pytorch_lightning.loggers import WandbLogger, CSVLogger
from pytorch_lightning.strategies import SingleDeviceStrategy, DDPStrategy
from torchmetrics.functional import mean_squared_error, auroc, mean_absolute_error
from sklearn.metrics import roc_auc_score, auc, precision_recall_curve
import os

logger = logging.getLogger("ConAN")


class TrainerHolder:
    """
    A class to hold the training configuration and setup for a machine learning experiment.
    """

    def __init__(
        self,
        config: Namespace,
        is_distributed: bool,
        device: torch.device,
        checkpoints_dir: str,
        logs_dir: str,
        monitor_set: str = "val",
    ):
        """
        Initializes the TrainerHolder with the given configuration and settings.

        Args:
            config (Namespace): Configuration for the experiment.
            is_distributed (bool): Flag indicating if the training is distributed.
            device (torch.device): Device on which the model will be trained.
            checkpoints_dir (str): Directory where checkpoints will be saved.
            logs_dir (str): Directory where logs will be saved.
            monitor_set (str, optional): The dataset set to monitor. Defaults to "val".
        """
        self.config = config
        self.device = device
        self.logs_dir = os.path.join(logs_dir, "metrics")
        if "Classification" in config.experiment.model_class.__name__:
            if config.trade_off:
                self.metric_to_monitor = f"{monitor_set}_{TrainerHolder.classification_metric_name(config.trade_off)[-1]}"
            else:
                self.metric_to_monitor = f"{monitor_set}_{TrainerHolder.classification_metric_name(config.trade_off)[0]}"
        else:
            self.metric_to_monitor = f"val_{TrainerHolder.regression_metric_name()}"

        self.is_distributed = is_distributed
        self.checkpoints_dir = checkpoints_dir
        self.lr_monitor = LearningRateMonitor(logging_interval="step")
        self.timer = Timer(verbose=True)
        self.monitor_set = monitor_set
        self.StochasticWeightAveraging = StochasticWeightAveraging(swa_lrs=1e-2)

    @staticmethod
    def regression_metric_name():
        """
        Returns:
            str: Name of the regression metric, which is "rmse".
        """
        return "rmse"

    @staticmethod
    def classification_metric_name(trade_off: bool = False):
        """
        Names of the classification metrics based on the trade-off flag.

        Args:
            trade_off (bool, optional): Flag indicating if trade-off metrics should be included. Defaults to False.

        Returns:
            list: List of classification metric names. If trade_off is True, returns ["auroc", "prc", "mean"].
                  If trade_off is False, returns ["auroc", "prc"].
        """
        if trade_off:
            return ["auroc", "prc", "mean"]
        else:
            return ["auroc", "prc"]

    @staticmethod
    def regression_metric(predicted, expected, squared: bool = False):
        """
        Calculates the regression error metric between the predicted and expected values.

        This function uses the Mean Squared Error (MSE) as the default metric to evaluate the 
        performance of a regression model. The function returns either the squared MSE (default) 
        or the root MSE, based on the `squared` parameter.

        Args:
            predicted: The predicted values generated by the model.
            expected: The true values that are expected.
            squared (bool): If True, returns the squared MSE (default behavior). 
                            If False, returns the root mean squared error (RMSE).

        Returns:
            float: The computed MSE or RMSE.
        """
        return mean_squared_error(predicted, expected, squared=squared)

    @staticmethod
    def classification_metric(predicted, expected, trade_off: bool = False):
        """
        Computes classification metrics including AUC and Precision-Recall AUC for model evaluation.

        This function calculates two key metrics for evaluating a classification model:
        - AUC (Area Under the ROC Curve)
        - Precision-Recall AUC

        If the `trade_off` parameter is set to True, the function also calculates the average of 
        these two metrics, which can be useful when needing a single balanced score.

        Args:
            predicted: The predicted probabilities or scores from the classification model.
            expected: The true class labels.
            trade_off (bool): If True, returns a list containing AUC, Precision-Recall AUC, and 
                            their average. If False, returns only AUC and Precision-Recall AUC.

        Returns:
            list: A list containing [AUC, Precision-Recall AUC] if `trade_off` is False, 
                or [AUC, Precision-Recall AUC, Average] if `trade_off` is True.
        """
        expected = expected.long()
        auc_score = roc_auc_score(
            y_true=expected.cpu().detach().numpy(), y_score=predicted.cpu().detach().numpy()
        )
        precision, recall, thresholds = precision_recall_curve(
            y_true=expected.cpu().detach().numpy(), probas_pred=predicted.cpu().detach().numpy()
        )
        prc_score = auc(recall, precision)
        if trade_off:
            metric = [auc_score, prc_score, (auc_score + prc_score) / 2.0]
        else:
            metric = [auc_score, prc_score]
        return metric


    def create_trainer(self, run_name: str, use_distributed_sampler: bool = True) -> pl.Trainer:
        """
        Creates and returns a PyTorch Lightning Trainer object configured based on the experiment settings.

        Args:
            run_name (str): Name of the current run for tracking purposes.
            use_distributed_sampler (bool, optional): Whether to use distributed sampler for training. Defaults to True.

        Returns:
            pl.Trainer: Initialized PyTorch Lightning Trainer object.
        """
        if "Classification" in self.config.experiment.model_class.__name__:
            task = "classification"
        else:
            task = "regression"
        callbacks = [
            self.early_stopping_callback(use_loss=True),
            self.checkpoint_callback(run_name, task=task),
            self.lr_monitor,
            self.timer,
        ]

        return pl.Trainer(
            max_epochs=self.config.num_epochs,
            logger=self.create_logger(run_name),
            log_every_n_steps=self.config.batch_size,
            callbacks=callbacks,
            strategy=self.training_strategy(),
            gradient_clip_val=1.0,
            default_root_dir=self.checkpoints_dir,
            num_sanity_val_steps=-1,
        )

    def create_logger(self, experiment_name=None) -> [pl.loggers.Logger]:
        """
        Creates and returns a list of PyTorch Lightning Logger objects.

        Args:
            experiment_name (str, optional): Name of the experiment. If not provided, a random name will be generated.

        Returns:
            list: List containing a CSVLogger instance initialized with the logs directory and experiment name.
        """
        if not experiment_name:
            experiment_name = "".join(random.choice(string.ascii_lowercase) for _ in range(15))
        loggers = [CSVLogger(self.logs_dir, experiment_name)]
        logger.info("*" * 100)
        logger.info(
            f"📈 Monitoring all metrics @ {os.path.join(self.logs_dir, experiment_name)} | Saving ckpt by {self.metric_to_monitor}"
        )
        logger.info("*" * 100)
        return loggers

    def early_stopping_callback(self, use_loss: bool = False) -> EarlyStopping:
        """
        Creates and returns an EarlyStopping callback object based on the configuration.

        Args:
            use_loss (bool, optional): Whether to monitor validation loss. Defaults to False.

        Returns:
            EarlyStopping: Initialized EarlyStopping callback object.
        """
        if use_loss:
            obj_monitor = "val_loss"
            mode = "min"
        else:
            obj_monitor = self.metric_to_monitor
            mode = "max"

        return EarlyStopping(
            monitor=obj_monitor,
            min_delta=self.config.early_stopping.min_delta,
            patience=self.config.early_stopping.patience,
            mode=mode,
            verbose=True,
            check_finite=True,
        )

    def checkpoint_callback(self, run_name: str, task: str = "regression") -> ModelCheckpoint:
        """
        Creates and returns a ModelCheckpoint callback object based on the task type.

        Args:
            run_name (str): Name of the current run for checkpointing purposes.
            task (str, optional): Type of task, either "regression" or "classification". Defaults to "regression".

        Returns:
            ModelCheckpoint: Initialized ModelCheckpoint callback object.
        """
        dirpath = os.path.join(self.checkpoints_dir, run_name)
        logger.info("*" * 100)
        logger.info(f"📦 Saving checkpoint @ {dirpath}")
        logger.info("*" * 100)
        if task == "regression":
            return ModelCheckpoint(
                dirpath=dirpath,  # Directory where the checkpoints will be saved
                filename="{epoch}-{step}-{val_rmse:.2f}",  # Checkpoint file name format
                verbose=True,  # Print a message when a new best checkpoint is saved
                monitor=self.metric_to_monitor,  # Metric to monitor for saving best checkpoints
                mode="min",  # Minimize the monitored metric (use "max" for metrics like accuracy)
                save_last=True,  # Save a checkpoint for the last epoch
            )
        elif task == "classification":
            if "mean" in self.metric_to_monitor:
                if self.monitor_set == "train":
                    return ModelCheckpoint(
                        dirpath=dirpath,  # Directory where the checkpoints will be saved
                        filename="{epoch}-{step}-{train_mean:.2f}",  # Checkpoint file name format
                        verbose=True,  # Print a message when a new best checkpoint is saved
                        monitor=self.metric_to_monitor,  # Metric to monitor for saving best checkpoints
                        mode="max",  # Minimize the monitored metric (use "max" for metrics like accuracy)
                        save_last=True,  # Save a checkpoint for the last epoch
                    )
                else:
                    return ModelCheckpoint(
                        dirpath=dirpath,  # Directory where the checkpoints will be saved
                        filename="{epoch}-{step}-{val_mean:.2f}",  # Checkpoint file name format
                        verbose=True,  # Print a message when a new best checkpoint is saved
                        monitor=self.metric_to_monitor,  # Metric to monitor for saving best checkpoints
                        mode="max",  # Minimize the monitored metric (use "max" for metrics like accuracy)
                        save_last=True,  # Save a checkpoint for the last epoch
                    )
            else:
                if self.monitor_set == "train":
                    return ModelCheckpoint(
                        dirpath=dirpath,  # Directory where the checkpoints will be saved
                        filename="{epoch}-{step}-{train_auroc:.2f}",  # Checkpoint file name format
                        verbose=True,  # Print a message when a new best checkpoint is saved
                        monitor=self.metric_to_monitor,  # Metric to monitor for saving best checkpoints
                        mode="max",  # Minimize the monitored metric (use "max" for metrics like accuracy)
                        save_last=True,  # Save a checkpoint for the last epoch
                    )
                else:
                    return ModelCheckpoint(
                        dirpath=dirpath,  # Directory where the checkpoints will be saved
                        filename="{epoch}-{step}-{val_auroc:.2f}",  # Checkpoint file name format
                        verbose=True,  # Print a message when a new best checkpoint is saved
                        monitor=self.metric_to_monitor,  # Metric to monitor for saving best checkpoints
                        mode="max",  # Minimize the monitored metric (use "max" for metrics like accuracy)
                        save_last=True,  # Save a checkpoint for the last epoch
                    )

    @staticmethod
    def gradient_accumulator_callback() -> GradientAccumulationScheduler:
        """
        Creates and returns a GradientAccumulationScheduler callback object with predefined scheduling.

        Returns:
            GradientAccumulationScheduler: Initialized GradientAccumulationScheduler object.
        """
        return GradientAccumulationScheduler(scheduling={0: 10, 4: 5, 10: 1})

    def training_strategy(self):
        """
        Determines and returns the appropriate training strategy based on the device type and distribution.

        Returns:
            Union[SingleDeviceStrategy, str]: Selected training strategy based on the device type and distribution.
        """
        if self.device.type == "cuda":
            if self.is_distributed:
                return "ddp_find_unused_parameters_false"
            else:
                return SingleDeviceStrategy(self.device, accelerator="cuda")
        elif self.device.type == "cpu":
            return SingleDeviceStrategy(self.device, accelerator="cpu")
        elif self.device.type == "mps":
            # return SingleDeviceStrategy(self.device, accelerator='mps')
            # A lot of operations are not supported on M1 yet
            return SingleDeviceStrategy(torch.device("cpu"), accelerator="cpu")
