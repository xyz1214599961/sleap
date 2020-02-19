"""SLEAP model training."""

import os
import attr
import argparse
import json
from typing import Callable, Dict, List, Optional, Text, Tuple, Union
from time import time
from datetime import datetime

import numpy as np
import tensorflow as tf

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sleap.nn import peak_finding

from sleap import Labels, Skeleton
from sleap.util import get_package_file
from sleap.nn import callbacks
from sleap.nn import data
from sleap.nn import job
from sleap.nn import model


class OHKMLoss(tf.keras.losses.Loss):
    """Online hard keypoint mining loss.

    This loss serves to dynamically reweight the MSE of the top-K worst channels in each
    batch. This is useful when fine tuning a model to improve performance on a hard
    part to optimize for (e.g., small, hard to see, often not visible).

    Note: This works with any type of channel, so it can work for PAFs as well.

    Attributes:
        K: Number of worst performing channels to compute loss for.
        weight: Scalar factor to multiply with the MSE for the top-K worst channels.
        name: Name of the loss tensor.
    """

    def __init__(self, K=2, weight=5, name="ohkm", **kwargs):
        super(OHKMLoss, self).__init__(name=name, **kwargs)
        self.K = K
        self.weight = weight

    def __call__(self, y_gt, y_pr, sample_weight=None):

        # Squared difference
        loss = tf.math.squared_difference(y_gt, y_pr)  # rank 4

        # Store initial shape for normalization
        batch_shape = tf.shape(loss)

        # Reduce over everything but channels axis
        loss = tf.reduce_sum(loss, axis=[0, 1, 2])

        # Keep only top loss terms
        k_vals, k_inds = tf.math.top_k(loss, k=self.K, sorted=False)

        # Apply weights
        k_loss = k_vals * self.weight

        # Reduce over all channels
        n_elements = tf.cast(
            batch_shape[0] * batch_shape[1] * batch_shape[2] * self.K, tf.float32
        )
        k_loss = tf.reduce_sum(k_loss) / n_elements

        return k_loss


class NodeLoss(tf.keras.metrics.Metric):
    """Compute node-specific loss.

    Useful for monitoring the MSE for specific body parts (channels).

    Attributes:
        node_ind: Index of channel to compute MSE for.
        name: Name of the loss tensor.
    """

    def __init__(self, node_ind, name="node_loss", **kwargs):
        super(NodeLoss, self).__init__(name=name, **kwargs)
        self.node_ind = node_ind
        self.node_mse = self.add_weight(
            name=name + ".mse", initializer="zeros", dtype=tf.float32
        )
        self.n_samples = self.add_weight(
            name=name + ".n_samples", initializer="zeros", dtype=tf.int32
        )
        self.height = self.add_weight(
            name=name + ".height", initializer="zeros", dtype=tf.int32
        )
        self.width = self.add_weight(
            name=name + ".width", initializer="zeros", dtype=tf.int32
        )

    def update_state(self, y_gt, y_pr, sample_weight=None):
        shape = tf.shape(y_gt)
        n_samples = shape[0]
        node_mse = tf.reduce_sum(
            tf.math.squared_difference(
                tf.gather(y_gt, self.node_ind, axis=3),
                tf.gather(y_pr, self.node_ind, axis=3),
            )
        )  # rank 4

        self.height.assign(shape[1])
        self.width.assign(shape[2])
        self.n_samples.assign_add(n_samples)
        self.node_mse.assign_add(node_mse)

    def result(self):
        return self.node_mse / tf.cast(
            self.n_samples * self.height * self.width, tf.float32
        )


@attr.s(auto_attribs=True)
class Trainer:
    training_job: job.TrainingJob

    tensorboard: bool = False
    tensorboard_freq: Union[Text, int] = "epoch"
    tensorboard_dir: Union[Text, None] = None
    tensorboard_profiling: bool = False
    zmq: bool = False
    control_zmq_port: int = 9000
    progress_report_zmq_port: int = 9001
    verbosity: int = 2
    save_viz: bool = False

    _img_shape: Tuple[int, int, int] = None
    _n_output_channels: int = None
    _train: data.TrainingData = None
    _val: data.TrainingData = None
    _test: data.TrainingData = None
    _ds_train: tf.data.Dataset = None
    _ds_val: tf.data.Dataset = None
    _ds_test: tf.data.Dataset = None
    _simple_skeleton: data.SimpleSkeleton = None
    _model: tf.keras.Model = None
    _optimizer: tf.keras.optimizers.Optimizer = None
    _loss_fn: tf.keras.losses.Loss = None
    _training_callbacks: List[tf.keras.callbacks.Callback] = None
    _history: dict = None

    @property
    def img_shape(self):
        return self._img_shape

    @property
    def n_output_channels(self):
        return self._n_output_channels

    @property
    def data_train(self):
        return self._train

    @property
    def data_val(self):
        return self._val

    @property
    def data_test(self):
        return self._test

    @property
    def ds_train(self):
        return self._ds_train

    @property
    def ds_val(self):
        return self._ds_val

    @property
    def ds_test(self):
        return self._ds_test

    @property
    def simple_skeleton(self):
        return self._simple_skeleton

    @property
    def model(self):
        return self._model

    @property
    def optimizer(self):
        return self._optimizer

    @property
    def loss_fn(self):
        return self._loss_fn

    @property
    def training_callbacks(self):
        return self._training_callbacks

    @property
    def history(self):
        return self._history

    def setup_data(
        self,
        labels_train: Union[Labels, Text] = None,
        labels_val: Union[Labels, Text] = None,
        labels_test: Union[Labels, Text] = None,
        data_train: Union[data.TrainingData, Text] = None,
        data_val: Union[data.TrainingData, Text] = None,
        data_test: Union[data.TrainingData, Text] = None,
    ):

        train = labels_train
        if train is None:
            train = self.training_job.train_set_filename
        if train is not None and isinstance(train, str):
            if self.verbosity > 0:
                print(f"Loading labels: {train}")
            train = Labels.load_file(train)
        if train is not None:
            train = data.TrainingData.from_labels(train)
        if train is None:
            train = data_train
        if train is not None and isinstance(train, str):
            if self.verbosity > 0:
                print(f"Loading data: {train}")
            train = data.TrainingData.load_file(train)
        if train is None:
            raise ValueError("Training data was not specified.")

        val = labels_val
        if val is None:
            val = self.training_job.val_set_filename
        if val is not None and isinstance(val, str):
            if self.verbosity > 0:
                print(f"Loading labels: {val}")
            val = Labels.load_file(val)
        if val is not None:
            val = data.TrainingData.from_labels(val)
        if val is None:
            val = data_val
        if val is not None and isinstance(val, str):
            if self.verbosity > 0:
                print(f"Loading data: {val}")
            val = data.TrainingData.load_file(val)
        if val is None and self.training_job.trainer.val_size is not None:
            train, val = data.split_training_data(
                train, first_split_fraction=self.training_job.trainer.val_size
            )
        if val is None:
            raise ValueError("Validation set or fraction must be specified.")

        test = labels_test
        if test is None:
            test = self.training_job.test_set_filename
        if test is not None and isinstance(test, str):
            if self.verbosity > 0:
                print(f"Loading labels: {test}")
            test = Labels.load_file(test)
        if test is not None:
            test = data.TrainingData.from_labels(test)
        if test is None:
            test = data_test
        if test is not None and isinstance(test, str):
            if self.verbosity > 0:
                print(f"Loading data: {test}")
            test = data.TrainingData.load_file(test)

        # Setup initial zipped datasets.
        ds_train = train.to_ds()
        ds_val = val.to_ds()
        ds_test = None
        if test is not None:
            ds_test = test.to_ds()

        # Adjust for input scaling and add padding to the model's minimum multiple.
        ds_train = data.adjust_dataset_input_scale(
            ds_train,
            input_scale=self.training_job.input_scale,
            min_multiple=self.training_job.model.input_min_multiple,
            normalize_image=False,
        )
        ds_val = data.adjust_dataset_input_scale(
            ds_val,
            input_scale=self.training_job.input_scale,
            min_multiple=self.training_job.model.input_min_multiple,
            normalize_image=False,
        )

        if ds_test is not None:
            ds_test = data.adjust_dataset_input_scale(
                ds_test,
                input_scale=self.training_job.input_scale,
                min_multiple=self.training_job.model.input_min_multiple,
                normalize_image=False,
            )

        # Cache the data with the current transformations.
        # ds_train = ds_train.cache()
        # ds_val = ds_val.cache()
        # if ds_test is not None:
        #     ds_test = ds_test.cache()

        # Apply augmentations.
        aug_params = dict(
            rotate=self.training_job.trainer.augment_rotate,
            rotation_min_angle=-self.training_job.trainer.augment_rotation,
            rotation_max_angle=self.training_job.trainer.augment_rotation,
            scale=self.training_job.trainer.augment_scale,
            scale_min=self.training_job.trainer.augment_scale_min,
            scale_max=self.training_job.trainer.augment_scale_max,
            uniform_noise=self.training_job.trainer.augment_uniform_noise,
            min_noise_val=self.training_job.trainer.augment_uniform_noise_min_val,
            max_noise_val=self.training_job.trainer.augment_uniform_noise_max_val,
            gaussian_noise=self.training_job.trainer.augment_gaussian_noise,
            gaussian_noise_mean=self.training_job.trainer.augment_gaussian_noise_mean,
            gaussian_noise_stddev=self.training_job.trainer.augment_gaussian_noise_stddev,
            contrast=self.training_job.trainer.augment_contrast,
            contrast_min_gamma=self.training_job.trainer.augment_contrast_min_gamma,
            contrast_max_gamma=self.training_job.trainer.augment_contrast_max_gamma,
            brightness=self.training_job.trainer.augment_brightness,
            brightness_val=self.training_job.trainer.augment_brightness_val,
        )
        ds_train = data.augment_dataset(ds_train, **aug_params)
        ds_val = data.augment_dataset(ds_val, **aug_params)

        if self.training_job.trainer.instance_crop:
            # Crop around instances.

            if (
                self.training_job.trainer.bounding_box_size is None
                or self.training_job.trainer.bounding_box_size <= 0
            ):
                # Estimate bounding box size from the data if not specified.
                # TODO: Do this earlier with more points if available.
                box_size = data.estimate_instance_crop_size(
                    train.points,
                    min_multiple=self.training_job.model.input_min_multiple,
                    padding=self.training_job.trainer.instance_crop_padding,
                )
                self.training_job.trainer.bounding_box_size = box_size

            crop_params = dict(
                box_height=self.training_job.trainer.bounding_box_size,
                box_width=self.training_job.trainer.bounding_box_size,
                use_ctr_node=self.training_job.trainer.instance_crop_use_ctr_node,
                ctr_node_ind=self.training_job.trainer.instance_crop_ctr_node_ind,
                normalize_image=True,
            )
            ds_train = data.instance_crop_dataset(ds_train, **crop_params)
            ds_val = data.instance_crop_dataset(ds_val, **crop_params)
            if ds_test is not None:
                ds_test = data.instance_crop_dataset(ds_test, **crop_params)

        else:
            # We're not instance cropping, so at this point the images are still not
            # normalized. Let's account for that before moving on.
            ds_train = data.normalize_dataset(ds_train)
            ds_val = data.normalize_dataset(ds_val)
            if ds_test is not None:
                ds_test = data.normalize_dataset(ds_test)

        # Setup remaining pipeline by output type.
        # rel_output_scale = (
        # self.training_job.model.output_scale / self.training_job.input_scale
        # )
        # TODO: Update this to the commented calculation above when model config
        # includes metadata about absolute input scale.
        rel_output_scale = self.training_job.model.output_scale
        output_type = self.training_job.model.output_type
        if output_type == model.ModelOutputType.CONFIDENCE_MAP:
            ds_train = data.make_confmap_dataset(
                ds_train,
                output_scale=rel_output_scale,
                sigma=self.training_job.trainer.sigma,
            )
            ds_val = data.make_confmap_dataset(
                ds_val,
                output_scale=rel_output_scale,
                sigma=self.training_job.trainer.sigma,
            )
            if ds_test is not None:
                ds_test = data.make_confmap_dataset(
                    ds_test,
                    output_scale=rel_output_scale,
                    sigma=self.training_job.trainer.sigma,
                )
            n_output_channels = train.skeleton.n_nodes

        elif output_type == model.ModelOutputType.TOPDOWN_CONFIDENCE_MAP:
            if not self.training_job.trainer.instance_crop:
                raise ValueError(
                    "Cannot train a topddown model without instance cropping enabled."
                )

            # TODO: Parametrize multiple heads in the training configuration.
            cm_params = dict(
                sigma=self.training_job.trainer.sigma,
                output_scale=rel_output_scale,
                with_instance_cms=False,
                with_all_peaks=False,
                with_ctr_peaks=True,
            )
            ds_train = data.make_instance_confmap_dataset(ds_train, **cm_params)
            ds_val = data.make_instance_confmap_dataset(ds_val, **cm_params)
            if ds_test is not None:
                ds_test = data.make_instance_confmap_dataset(ds_test, **cm_params)
            n_output_channels = train.skeleton.n_nodes

        elif output_type == model.ModelOutputType.PART_AFFINITY_FIELD:
            ds_train = data.make_paf_dataset(
                ds_train,
                train.skeleton.edges,
                output_scale=rel_output_scale,
                distance_threshold=self.training_job.trainer.sigma,
            )
            ds_val = data.make_paf_dataset(
                ds_val,
                train.skeleton.edges,
                output_scale=rel_output_scale,
                distance_threshold=self.training_job.trainer.sigma,
            )
            if ds_test is not None:
                ds_test = data.make_paf_dataset(
                    ds_test,
                    train.skeleton.edges,
                    output_scale=rel_output_scale,
                    distance_threshold=self.training_job.trainer.sigma,
                )
            n_output_channels = train.skeleton.n_edges * 2

        elif output_type == model.ModelOutputType.CENTROIDS:
            cm_params = dict(
                sigma=self.training_job.trainer.sigma,
                output_scale=rel_output_scale,
                use_ctr_node=self.training_job.trainer.instance_crop_use_ctr_node,
                ctr_node_ind=self.training_job.trainer.instance_crop_ctr_node_ind,
            )

            ds_train = data.make_centroid_confmap_dataset(ds_train, **cm_params)
            ds_val = data.make_centroid_confmap_dataset(ds_val, **cm_params)
            if ds_test is not None:
                ds_test = data.make_centroid_confmap_dataset(ds_test, **cm_params)

            n_output_channels = 1

        else:
            raise ValueError(
                f"Invalid model output type specified ({self.training_job.model.output_type})."
            )

        if self.training_job.trainer.steps_per_epoch <= 0:
            self.training_job.trainer.steps_per_epoch = int(
                len(train.images) // self.training_job.trainer.batch_size
            )
        if self.training_job.trainer.val_steps_per_epoch <= 0:
            self.training_job.trainer.val_steps_per_epoch = int(
                np.ceil(len(val.images) / self.training_job.trainer.batch_size)
            )

        # Set up shuffling, batching, repeating and prefetching.
        shuffle_buffer_size = self.training_job.trainer.shuffle_buffer_size
        if shuffle_buffer_size is None or shuffle_buffer_size <= 0:
            shuffle_buffer_size = len(train.images)
        ds_train = (
            ds_train.shuffle(shuffle_buffer_size)
            .repeat(-1)
            .batch(self.training_job.trainer.batch_size, drop_remainder=True)
            .prefetch(buffer_size=self.training_job.trainer.steps_per_epoch)
            # .prefetch(buffer_size=tf.data.experimental.AUTOTUNE)
        )
        ds_val = (
            ds_val.repeat(-1)
            .batch(self.training_job.trainer.batch_size)
            .prefetch(buffer_size=tf.data.experimental.AUTOTUNE)
        )
        if ds_test is not None:
            ds_test = ds_val.batch(self.training_job.trainer.batch_size).prefetch(
                buffer_size=self.training_job.trainer.val_steps_per_epoch
                # buffer_size=tf.data.experimental.AUTOTUNE
            )

        # Get image shape after all the dataset transformations are applied.
        img_shape = list(ds_val.take(1))[0][0][0].shape

        # Update internal attributes.
        self._img_shape = img_shape
        self._n_output_channels = n_output_channels
        self._train = train
        self._val = val
        self._test = test
        self._ds_train = ds_train
        self._ds_val = ds_val
        self._ds_test = ds_test
        self._simple_skeleton = train.skeleton

        if (
            self.training_job.model.skeletons is None
            or len(self.training_job.model.skeletons) == 0
        ):
            # Save skeleton to training job/model config if none were already stored.
            skeleton = Skeleton.from_names_and_edge_inds(
                node_names=self.simple_skeleton.node_names,
                edge_inds=self.simple_skeleton.edge_inds,
            )
            self.training_job.model.skeletons = [skeleton]

        if self.verbosity > 0:
            print("Data:")
            print("  Input scale:", self.training_job.input_scale)
            print("  Relative output scale:", self.training_job.model.output_scale)
            print(
                "  Output scale:",
                self.training_job.input_scale * self.training_job.model.output_scale,
            )
            print("  Training data:", self.data_train.images.shape)
            print("  Validation data:", self.data_val.images.shape)
            if self.data_test is not None:
                print("  Test data:", self.data_test.images.shape)
            else:
                print("  Test data: N/A")
            print("  Image shape:", self.img_shape)
            print("  Output channels:", self.n_output_channels)
            print("  Skeleton:", self.simple_skeleton)
            print()

        return ds_train, ds_val, ds_test

    def setup_callbacks(self) -> List[tf.keras.callbacks.Callback]:

        callback_list = []

        if self.training_job.trainer.reduce_lr_on_plateau:
            callback_list.append(
                callbacks.ReduceLROnPlateau(
                    min_delta=self.training_job.trainer.reduce_lr_min_delta,
                    factor=self.training_job.trainer.reduce_lr_factor,
                    patience=self.training_job.trainer.reduce_lr_patience,
                    cooldown=self.training_job.trainer.reduce_lr_cooldown,
                    min_lr=self.training_job.trainer.reduce_lr_min_lr,
                    monitor=self.training_job.trainer.monitor_metric_name,
                    mode="auto",
                    verbose=self.verbosity,
                )
            )

        if self.training_job.trainer.early_stopping:
            callback_list.append(
                callbacks.EarlyStopping(
                    monitor=self.training_job.trainer.monitor_metric_name,
                    min_delta=self.training_job.trainer.early_stopping_min_delta,
                    patience=self.training_job.trainer.early_stopping_patience,
                    verbose=self.verbosity,
                    mode="min",
                )
            )

        if self.training_job.run_path is not None:

            if self.save_viz:
                if self.training_job.model.output_type in [
                    model.ModelOutputType.CONFIDENCE_MAP,
                    model.ModelOutputType.TOPDOWN_CONFIDENCE_MAP,
                    model.ModelOutputType.CENTROIDS,
                ]:
                    callback_list.append(
                        callbacks.MatplotlibSaver(
                            save_folder=os.path.join(self.training_job.run_path, "viz"),
                            plot_fn=lambda: self.visualize_predictions(
                                training_set=True
                            ),
                            prefix="train",
                        )
                    )
                    callback_list.append(
                        callbacks.MatplotlibSaver(
                            save_folder=os.path.join(self.training_job.run_path, "viz"),
                            plot_fn=lambda: self.visualize_predictions(
                                training_set=False
                            ),
                            prefix="val",
                        )
                    )

            if self.training_job.trainer.csv_logging:
                callback_list.append(
                    callbacks.CSVLogger(
                        filename=os.path.join(
                            self.training_job.run_path,
                            self.training_job.trainer.csv_log_filename,
                        )
                    )
                )

            if self.tensorboard:
                if self.tensorboard_dir is None:
                    self.tensorboard_dir = self.training_job.run_path
                callback_list.append(
                    callbacks.TensorBoard(
                        log_dir=self.tensorboard_dir,
                        update_freq=self.tensorboard_freq,
                        profile_batch=2 if self.tensorboard_profiling else 0,
                    )
                )

                if self.training_job.model.output_type in [
                    model.ModelOutputType.CONFIDENCE_MAP,
                    model.ModelOutputType.TOPDOWN_CONFIDENCE_MAP,
                    model.ModelOutputType.CENTROIDS,
                ]:
                    callback_list.append(
                        callbacks.TensorBoardMatplotlibWriter(
                            log_dir=os.path.join(self.tensorboard_dir, "train"),
                            plot_fn=lambda: self.visualize_predictions(
                                training_set=True
                            ),
                            tag="viz_train",
                        )
                    )
                    callback_list.append(
                        callbacks.TensorBoardMatplotlibWriter(
                            log_dir=os.path.join(self.tensorboard_dir, "validation"),
                            plot_fn=lambda: self.visualize_predictions(
                                training_set=False
                            ),
                            tag="viz_val",
                        )
                    )

            if self.training_job.trainer.save_every_epoch:
                if self.training_job.newest_model_filename is None:
                    self.training_job.newest_model_filename = "newest_model.h5"

                callback_list.append(
                    callbacks.ModelCheckpoint(
                        filepath=os.path.join(
                            self.training_job.run_path,
                            self.training_job.newest_model_filename,
                        ),
                        monitor=self.training_job.trainer.monitor_metric_name,
                        save_best_only=False,
                        save_weights_only=False,
                        save_freq="epoch",
                        verbose=self.verbosity,
                    )
                )

            if self.training_job.trainer.save_best_val:
                if self.training_job.best_model_filename is None:
                    self.training_job.best_model_filename = "best_model.h5"

                callback_list.append(
                    callbacks.ModelCheckpoint(
                        filepath=os.path.join(
                            self.training_job.run_path,
                            self.training_job.best_model_filename,
                        ),
                        monitor=self.training_job.trainer.monitor_metric_name,
                        save_best_only=True,
                        save_weights_only=False,
                        save_freq="epoch",
                        verbose=self.verbosity,
                    )
                )

            if self.training_job.trainer.save_final_model:
                if self.training_job.final_model_filename is None:
                    self.training_job.final_model_filename = "final_model.h5"

                callback_list.append(
                    callbacks.ModelCheckpointOnEvent(
                        filepath=os.path.join(
                            self.training_job.run_path,
                            self.training_job.final_model_filename,
                        ),
                        event="train_end",
                    )
                )

            self.training_job.save(
                os.path.join(self.training_job.run_path, "training_job.json")
            )

        if self.zmq:
            # Callbacks: ZMQ control
            if self.control_zmq_port is not None:
                callback_list.append(
                    callbacks.TrainingControllerZMQ(
                        address="tcp://127.0.0.1",
                        port=self.control_zmq_port,
                        topic="",
                        poll_timeout=10,
                    )
                )

            # Callbacks: ZMQ progress reporter
            if self.progress_report_zmq_port is not None:
                callback_list.append(
                    callbacks.ProgressReporterZMQ(
                        port=self.progress_report_zmq_port,
                        what=str(self.training_job.model.output_type),
                    )
                )

        self._training_callbacks = callback_list
        return callback_list

    def setup_optimization(self):

        if self.training_job.trainer.optimizer.lower() == "adam":
            optimizer = tf.keras.optimizers.Adam(
                learning_rate=self.training_job.trainer.learning_rate,
                amsgrad=self.training_job.trainer.amsgrad,
            )

        elif self.training_job.trainer.optimizer.lower() == "rmsprop":
            optimizer = tf.keras.optimizers.Adam(
                learning_rate=self.training_job.trainer.learning_rate
            )

        else:
            raise ValueError(
                "Unrecognized optimizer specified: %s",
                self.training_job.trainer.optimizer,
            )

        # Construct loss and metrics.
        metrics = []
        mse_loss = tf.keras.losses.MeanSquaredError()

        if (
            self.training_job.trainer.ohkm
            and self.training_job.model.output_type != model.ModelOutputType.CENTROIDS
        ):
            # Add OHKM loss if not training centroids (they have a single channel).
            ohkm_loss = OHKMLoss(
                K=min(self.training_job.trainer.ohkm_K, self.n_output_channels - 1),
                weight=self.training_job.trainer.ohkm_weight,
            )

            def loss_fn(y_gt, y_pr):
                return mse_loss(y_gt, y_pr) + ohkm_loss(y_gt, y_pr)

            metrics.append(ohkm_loss)

        else:
            loss_fn = mse_loss

        if (
            self.training_job.trainer.node_metrics
            and self.training_job.model.output_type
            in [
                model.ModelOutputType.CONFIDENCE_MAP,
                model.ModelOutputType.TOPDOWN_CONFIDENCE_MAP,
            ]
        ):
            # Add node-wise metrics when training confidence map models.
            for node_ind, node_name in enumerate(self.simple_skeleton.node_names):
                metrics.append(NodeLoss(node_ind=node_ind, name=node_name))

        self._optimizer = optimizer
        self._loss_fn = loss_fn
        self._metrics = metrics

        return optimizer, loss_fn

    def setup_model(self, img_shape=None, n_output_channels=None):

        if img_shape is None:
            img_shape = self.img_shape

        if n_output_channels is None:
            n_output_channels = self.n_output_channels

        input_layer = tf.keras.layers.Input(img_shape, name="input")

        outputs = self.training_job.model.output(input_layer, n_output_channels)
        if isinstance(outputs, tf.keras.Model):
            outputs = outputs.outputs

        keras_model = tf.keras.Model(
            input_layer, outputs, name=self.training_job.model.backbone_name
        )

        if self.verbosity > 0:
            print(f"Model: {keras_model.name}")
            print(f"  Input: {keras_model.input_shape}")
            print(f"  Output: {keras_model.output_shape}")
            print(f"  Layers: {len(keras_model.layers)}")
            print(f"  Params: {keras_model.count_params():3,}")
            print()

        self._model = keras_model

        return keras_model

    def train(
        self,
        labels_train: Union[Labels, Text] = None,
        labels_val: Union[Labels, Text] = None,
        labels_test: Union[Labels, Text] = None,
        data_train: Union[data.TrainingData, Text] = None,
        data_val: Union[data.TrainingData, Text] = None,
        data_test: Union[data.TrainingData, Text] = None,
        extra_callbacks: List[tf.keras.callbacks.Callback] = None,
    ) -> tf.keras.Model:

        self.setup_data(
            labels_train=labels_train,
            labels_val=labels_val,
            labels_test=labels_test,
            data_train=data_train,
            data_val=data_val,
            data_test=data_test,
        )
        self.setup_model()
        self.setup_optimization()

        if (
            self.training_job.save_dir is not None
            and self.training_job.run_name is None
        ):
            # Generate new run name if save_dir specified but not the run name.
            self.training_job.run_name = self.training_job.new_run_name(
                suffix=f"n={len(self.data_train.images)}"
            )

        if self.training_job.run_path is not None:
            if not os.path.exists(self.training_job.run_path):
                os.makedirs(self.training_job.run_path, exist_ok=True)
            if self.verbosity > 0:
                print(f"Run path: {self.training_job.run_path}")
        else:
            if self.verbosity > 0:
                print(f"Run path: Not provided, nothing will be saved to disk.")

        self.setup_callbacks()
        if extra_callbacks is not None:
            self.training_callbacks.extend(extra_callbacks)
        self.model.compile(
            optimizer=self.optimizer, loss=self.loss_fn, metrics=self._metrics
        )

        t0 = datetime.now()
        if self.verbosity > 0:
            print(f"Training started: {str(t0)}")

        self._history = self.model.fit(
            self.ds_train,
            epochs=self.training_job.trainer.num_epochs,
            callbacks=self.training_callbacks,
            validation_data=self.ds_val,
            steps_per_epoch=self.training_job.trainer.steps_per_epoch,
            validation_steps=self.training_job.trainer.val_steps_per_epoch,
            verbose=self.verbosity,
        )
        t1 = datetime.now()
        elapsed = t1 - t0
        if self.verbosity > 0:
            print(f"Training finished: {str(t1)}")
            print(f"Total runtime: {str(elapsed)}")

        # TODO: Evaluate final test set performance if available

        return self.model

    @classmethod
    def set_run_name(
        cls, training_job: job.TrainingJob, labels_filename: str,
    ):
        training_job.save_dir = os.path.join(os.path.dirname(labels_filename), "models")
        training_job.run_name = training_job.new_run_name(check_existing=True)

        # Make the run directory if it doesn't exist, since when we load the
        # saved job the save_dir will be reset if the dir doesn't exist
        os.makedirs(training_job.run_path)

    @classmethod
    def train_subprocess(
        cls,
        training_job: job.TrainingJob,
        labels_filename: str,
        waiting_callback: Optional[Callable] = None,
        update_run_name: bool = True,
        save_viz: bool = False,
    ):
        """Runs training inside subprocess."""
        import subprocess as sub
        import time
        import tempfile

        if update_run_name:
            cls.set_run_name(training_job, labels_filename)

        run_name = training_job.run_name
        run_path = training_job.run_path

        with tempfile.TemporaryDirectory() as temp_dir:

            # Write a temporary file of the TrainingJob so that we can respect
            # any changed made to the job attributes after it was loaded.
            temp_filename = (
                datetime.now().strftime("%y%m%d_%H%M%S") + "_training_job.json"
            )
            training_job_path = os.path.join(temp_dir, temp_filename)
            job.TrainingJob.save_json(training_job, training_job_path)

            # Build CLI arguments for training
            cli_args = [
                "python",
                "-m",
                "sleap.nn.training",
                training_job_path,
                labels_filename,
                "--zmq",
                "--run_name",
                run_name,
            ]

            if save_viz:
                cli_args.append("--save_viz")

            print(cli_args)

            # Run training in a subprocess
            # with sub.Popen(cli_args, stdout=sub.PIPE) as proc:
            with sub.Popen(cli_args) as proc:

                # Wait till training is done, calling a callback if given.
                while proc.poll() is None:
                    if waiting_callback is not None:
                        if waiting_callback() == -1:
                            # -1 signals user cancellation
                            return "", False
                    time.sleep(0.1)

                success = proc.returncode == 0

        return run_path, success

    def visualize_predictions(
        self, training_set: bool = False, figsize=(6, 6)
    ) -> matplotlib.figure.Figure:
        """Plot confidence map visualizations.

        This method is primarily intended to be used as a callback for TensorBoard or
        other forms of visualization during training.

        Args:
            training_set: If True, will sample from the training set for data to visualize.
                If False, the validation set will be sampled.
            figsize: Size of the output figure (width, height) in inches. See `plt.figure`
                for more info on sizing.

        Returns:
            A `matplotlib.figure.Figure` instance. This object provides `show` and
            `savefig` methods which can be used to display or render the visualized
            plots.

            This will NOT display any figure until one of the above methods are called.
        """
        topdown = (
            self.training_job.model.output_type
            == model.ModelOutputType.TOPDOWN_CONFIDENCE_MAP
        )

        # Draw a batch from the specified dataset.
        if training_set:
            X, Y_gt = next(iter(self.ds_train))
        else:
            X, Y_gt = next(iter(self.ds_val))

        # Select a single sample from the batch.
        ind = np.random.randint(0, len(X))
        X = X[ind : (ind + 1)]

        if isinstance(Y_gt, (list, tuple)):
            Y_gt = Y_gt[0]
        Y_gt = Y_gt[ind : (ind + 1)]

        stop_training = self.model.stop_training

        # Predict on single sample.
        Y_pr = self.model.predict(X)
        cm_scale = Y_pr.shape[1] / X.shape[1]

        self.model.stop_training = stop_training

        # Find peaks.
        if topdown:
            peaks_gt, peak_vals_gt = peak_finding.find_global_peaks(Y_gt)
            peaks_pr, peak_vals_pr = peak_finding.find_global_peaks(Y_pr)
            peaks_gt = peaks_gt.numpy() / cm_scale
            peaks_pr = peaks_pr.numpy() / cm_scale
            peaks_gt[peak_vals_gt < 0.3, :] = np.nan
            peaks_pr[peak_vals_pr < 0.3, :] = np.nan

        else:
            peaks_gt = peak_finding.find_local_peaks(Y_gt)[0].numpy() / cm_scale
            peaks_pr = peak_finding.find_local_peaks(Y_pr)[0].numpy() / cm_scale

        # Drop singleton sample dimension.
        img = X[0]
        cm = Y_pr[0]

        # Plot.
        fig = plt.figure(figsize=figsize)
        ax = fig.add_axes([0, 0, 1, 1], frameon=False)
        ax.get_xaxis().set_visible(False)
        ax.get_yaxis().set_visible(False)
        plt.autoscale(tight=True)
        ax.imshow(
            np.squeeze(img),
            cmap="gray",
            origin="lower",
            extent=[-0.5, img.shape[1] - 0.5, -0.5, img.shape[0] - 0.5],
        )
        ax.imshow(
            np.squeeze(Y_pr.max(axis=-1)),
            alpha=0.5,
            origin="lower",
            extent=[-0.5, img.shape[1] - 0.5, -0.5, img.shape[0] - 0.5],
        )

        if topdown:
            # Draw error line for topdown since no matching is required as
            # (there is only one peak per node type).
            for p_gt, p_pr in zip(peaks_gt, peaks_pr):
                ax.plot([p_gt[2], p_pr[2]], [p_gt[1], p_pr[1]], "r-", alpha=0.5, lw=2)

        ax.plot(peaks_gt[:, 2], peaks_gt[:, 1], ".", alpha=0.6, ms=10)
        ax.plot(peaks_pr[:, 2], peaks_pr[:, 1], ".", alpha=0.6, ms=10)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.grid(False)

        return fig


def main():
    """CLI for training."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "training_job_path", help="Path to training job profile JSON file."
    )
    parser.add_argument("labels_path", help="Path to labels file to use for training.")
    parser.add_argument(
        "--val_labels",
        "--val",
        help="Path to labels file to use for validation (overrides training job path if set).",
    )
    parser.add_argument(
        "--test_labels",
        "--test",
        help="Path to labels file to use for test (overrides training job path if set).",
    )
    parser.add_argument(
        "--tensorboard",
        action="store_true",
        help="Enables TensorBoard logging to the run path.",
    )
    parser.add_argument(
        "--save_viz",
        action="store_true",
        help="Enables saving of prediction visualizations to the run folder.",
    )
    parser.add_argument(
        "--zmq", action="store_true", help="Enables ZMQ logging (for GUI)."
    )
    parser.add_argument(
        "--run_name",
        default="",
        help="Run name to use when saving file, overrides other run name settings.",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        help="Prefix to prepend to run name. Can be specified multiple times.",
    )
    parser.add_argument(
        "--suffix",
        action="append",
        help="Suffix to append to run name. Can be specified multiple times.",
    )

    args, _ = parser.parse_known_args()

    job_filename = args.training_job_path
    if not os.path.exists(job_filename):
        profile_dir = get_package_file("sleap/training_profiles")

        if os.path.exists(os.path.join(profile_dir, job_filename)):
            job_filename = os.path.join(profile_dir, job_filename)
        else:
            raise FileNotFoundError(f"Could not find training profile: {job_filename}")

    labels_train_path = args.labels_path

    training_job = job.TrainingJob.load_json(job_filename)

    # Set data paths in job.
    training_job.labels_filename = labels_train_path
    if args.val_labels is not None:
        training_job.val_set_filename = args.val_labels
    if args.test_labels is not None:
        training_job.test_set_filename = args.test_labels

    if training_job.save_dir is None:
        # Default save dir to models subdir of training labels.
        training_job.save_dir = os.path.join(
            os.path.dirname(labels_train_path), "models"
        )

    if args.run_name:
        training_job.run_name = args.run_name
    else:
        prefixes = args.prefix or []
        if training_job.run_name is not None:
            # Add run name specified in file to prefixes.
            prefixes.append(training_job.run_name)

        # Create new run name.
        training_job.run_name = training_job.new_run_name(
            prefix=prefixes, suffix=args.suffix, check_existing=True
        )

    # Print path to training job run.

    # NOTE: This must be first line printed to stdout, otherwise we won't be
    # able to access it when running training in subprocess via Popen.

    print(training_job.run_path)
    print()

    print(f"Training labels file: {labels_train_path}")
    print(f"Training profile: {job_filename}")
    print()

    # Log configuration to console.
    print("Arguments:")
    print(json.dumps(vars(args), indent=4))
    print()
    print("Training job:")
    print(json.dumps(job.TrainingJob._to_dicts(training_job), indent=4))
    print()

    print("Initializing training...")
    # Create a trainer and run!
    trainer = Trainer(
        training_job,
        tensorboard=args.tensorboard,
        save_viz=args.save_viz,
        zmq=args.zmq,
        verbosity=2,
    )

    trained_model = trainer.train()


if __name__ == "__main__":
    main()