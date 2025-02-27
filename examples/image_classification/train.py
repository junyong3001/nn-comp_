import math
import os
import logging

from tqdm import tqdm
from tensorflow import keras
import tensorflow as tf
import tensorflow_datasets as tfds

import random

from dataloader import dataset_factory
from utils import callbacks as custom_callbacks
from utils import optimizer_factory

import horovod.tensorflow as hvd

from models.loss import BespokeTaskLoss, accuracy
from prep import add_augmentation

# constants
epochs = 50

def load_data_nvidia(dataset, model_handler, sampling_ratio=1.0, training_augment=True, batch_size=-1, n_classes=100, cutmix_alpha=0.0, mixup_alpha=0.0):

    dim = (model_handler.height, model_handler.width)

    if batch_size == -1:
        batch_size = model_handler.get_batch_size(dataset)

    augmenter = "autoaugment"
    augmenter = None
    augmenter_params = {}
    #augmenter_params["cutout_const"] = None
    #augmenter_params["translate_const"] = None
    #augmenter_params["num_layers"] = None
    #augmenter_params["magnitude"] = None
    #augmenter_params["autoaugmentation_name"] = None

    builders = []
    builders.append(dataset_factory.Dataset(
        dataset=dataset,
        index_file_dir=None,
        split="train",
        image_size=dim[0],
        num_classes=n_classes,
        num_channels=3,
        batch_size=batch_size,
        dtype='float32',
        one_hot=True,
        use_dali=False,
        augmenter=augmenter,
        cache=False,
        mean_subtract=False,
        standardize=False,
        augmenter_params=augmenter_params,
        cutmix_alpha=cutmix_alpha, 
        mixup_alpha=mixup_alpha,
        defer_img_mixing=True,
        data_preprocess_func=lambda x:model_handler.data_preprocess_func(x, None),
        model_preprocess_func=lambda x:model_handler.model_preprocess_func(x, None),
        disable_map_parallelization=False))

    val_split = "test"
    if dataset == "imagenet2012":
        val_split = "validation"

    builders.append(dataset_factory.Dataset(
        dataset=dataset,
        index_file_dir=None,
        split=val_split,
        image_size=dim[0],
        num_classes=n_classes,
        num_channels=3,
        batch_size=batch_size,
        dtype='float32',
        one_hot=True,
        use_dali=False,
        augmenter=None,
        cache=False,
        mean_subtract=False,
        standardize=False,
        augmenter_params=None,
        cutmix_alpha=0.0, 
        mixup_alpha=0.0,
        defer_img_mixing=False,
        hvd_size=hvd.size(),
        data_preprocess_func=lambda x:model_handler.data_preprocess_func(x, None),
        model_preprocess_func=lambda x:model_handler.model_preprocess_func(x, None),
        disable_map_parallelization=False))

    return [ builder.build() for builder in builders ]

def load_dataset(dataset, model_handler, sampling_ratio=1.0, training_augment=True, n_classes=100):

    batch_size = model_handler.get_batch_size(dataset)

    if dataset in ["imagenet2012", "cifar100", "caltech_birds2011", "oxford_iiit_pet"]:
        train_data_generator, valid_data_generator = load_data_nvidia(dataset, model_handler, sampling_ratio=sampling_ratio, training_augment=training_augment, n_classes=n_classes)

        if dataset == "imagenet2012": 
            num_train_examples = 1281167
            num_val_examples = 50000
        elif dataset == "cifar100":
            num_train_examples = 50000
            num_val_examples = 10000
        elif dataset == "caltech_birds2011":
            num_train_examples = 5994
            num_val_examples = 5794
        else:
            num_train_examples = 3680
            num_val_examples = 3669

        if sampling_ratio != 1.0:
            sampling_count = (int(num_train_examples * sampling_ratio), int(num_val_examples * sampling_ratio))
        else:
            sampling_count = (None, None)

        iters = num_train_examples // (batch_size * hvd.size())
        iters_val = num_val_examples // (batch_size * hvd.size())
        test_data_generator = valid_data_generator

    else:
        train_data_generator, valid_data_generator, test_data_generator = load_data(dataset, model_handler, sampling_ratio=sampling_ratio, training_augment=training_augment, n_classes=n_classes)
        iters = len(train_data_generator)
        iters_val = len(valid_data_generator)

    return (train_data_generator, valid_data_generator, test_data_generator), (iters, iters_val)



def train(dataset, model, model_name, model_handler, run_eagerly=False, callbacks=None, is_training=True, augment=True, exclude_val=False, save_dir=None, n_classes=100, conf=None, epochs_=None, sampling_ratio=1.0, save_all=False, use_unique_name=False):

    import horovod.tensorflow.keras as hvd_

    batch_size = model_handler.get_batch_size(dataset)

    if type(dataset) == str:
        data_gen, iters_info = load_dataset(dataset, model_handler, sampling_ratio=sampling_ratio, training_augment=augment, n_classes=n_classes)
    else:
        data_gen, iters_info = dataset
    train_data_generator, valid_data_generator, test_data_generator = data_gen
    iters, iters_val = iters_info
    iters = int(iters * sampling_ratio)

    if epochs_ is None:
        if is_training and hasattr(model_handler, "get_train_epochs"):
            epochs_ = model_handler.get_train_epochs()
        else:
            epochs_ = epochs

    if callbacks is None:   
        callbacks = []

    if conf is not None:
        callbacks.append(hvd_.callbacks.BroadcastGlobalVariablesCallback(0))
        callbacks.append(hvd_.callbacks.MetricAverageCallback())

        lr_params = {
            'name':conf["lr_name"],
            'initial_lr': conf["initial_lr"],
            'decay_epochs': conf["decay_epochs"],
            'decay_rate': conf["decay_rate"],
            'warmup_epochs': conf["warmup_epochs"],
            'examples_per_epoch': None,
            'boundaries': None,
            'multipliers': None,
            'scale_by_batch_size': 1./float(batch_size),
            'staircase': True,
            't_mul': conf["t_mul"],
            'm_mul': conf["m_mul"],
            'alpha': conf['alpha']
        }

        learning_rate = optimizer_factory.build_learning_rate(
            params=lr_params,
            batch_size=batch_size * hvd_.size() * conf["grad_accum_steps"], # updates are iteration based not batch-index based
            train_steps=iters,
            max_epochs=epochs)

        opt_params = {
            'name': conf["opt_name"],
            'decay': conf["decay"],
            'epsilon': conf["epsilon"],
            'lookahead': conf["lookahead"],
            'momentum': conf["momentum"],
            'moving_average_decay': conf["moving_average_decay"],
            'nesterov': conf["nesterov"],
            'beta_1': conf["beta_1"],
            'beta_2': conf["beta_2"],

        }
        
        # set up optimizer
        optimizer = optimizer_factory.build_optimizer(
            optimizer_name=conf["opt_name"],
            base_learning_rate=learning_rate,
            params=opt_params
        )

        if conf["use_amp"] and conf["grad_accum_steps"] > 1:
            optimizer = tf.keras.mixed_precision.LossScaleOptimizer(optimizer)
        elif conf["grad_accum_steps"] == 1:
            optimizer = hvd_.DistributedOptimizer(optimizer, compression=hvd_.Compression.fp16 if conf["hvd_fp16_compression"] else hvd_.Compression.none)

        # compile
        if "distillation" in conf["mode"]:
            mute = "free" in conf["mode"]
            loss = {model.output[0].name.split("/")[0]:BespokeTaskLoss(mute=mute)}
            metrics={model.output[0].name.split("/")[0]:accuracy}
            model.compile(optimizer=optimizer, loss=loss, metrics=metrics, run_eagerly=False, experimental_run_tf_function=False)
        else:
            loss = tf.keras.losses.CategoricalCrossentropy(label_smoothing=conf["label_smoothing"])
            model.compile(optimizer=optimizer, loss=loss, metrics=['accuracy'], run_eagerly=False, experimental_run_tf_function=False)

        if conf["moving_average_decay"] > 0:
            callbacks.append(
                custom_callbacks.MovingAverageCallback(intratrain_eval_using_ema=conf["intratrain_eval_using_ema"]))

    else:
        pass

    print(model.count_params())

    if save_dir is not None and hvd_.local_rank() == 0:
        if use_unique_name:
            model_name_ = '%s_model.best.h5' % (model_name+"_"+dataset)
        else:
            model_name_ = '%s_model.{epoch:03d}.h5' % (model_name+"_"+dataset)
        if not os.path.isdir(save_dir):
            os.makedirs(save_dir)
        filepath = os.path.join(save_dir, model_name_)

        if conf is not None and conf["moving_average_decay"] > 0:
            mchk = custom_callbacks.AverageModelCheckpoint(update_weights=False,
                                          filepath=filepath,
                                          monitor="val_accuracy",
                                          verbose=1,
                                          save_best_only=not save_all,
                                          save_weights_only=False,
                                          mode="auto",
                                          save_freq="epoch")
        else:
            mchk = keras.callbacks.ModelCheckpoint(
                filepath=filepath,
                monitor="val_accuracy",
                verbose=0,
                save_best_only=True,
                save_weights_only=False,
                mode="auto",
                save_freq="epoch",
                options=None,
            )
        callbacks.append(mchk)


    if exclude_val:
        model_history = model.fit(train_data_generator,
                                        callbacks=callbacks,
                                        verbose=1 if hvd_.rank() == 0 else 0,
                                        epochs=epochs_,
                                        steps_per_epoch=iters)
    else:
        model_history = model.fit(train_data_generator,
                                        validation_data=valid_data_generator,
                                        callbacks=callbacks,
                                        verbose=1 if hvd_.rank() == 0 else 0,
                                        epochs=epochs_,
                                        steps_per_epoch=iters)

    return model


def train_step(X, model, teacher_logits=None, y=None, ret_last_tensor=False):

    if teacher_logits is not None:
        len_outputs = len(teacher_logits)
    else:
        len_outputs = 0

    with tf.GradientTape() as tape:
        logits = model(X)
        if type(logits) != list:
            logits = [logits]
        if len_outputs > 0:
            loss = None
            for i in range(len_outputs-1): # exclude the model's ouptut logit.
                if type(logits[i+1]) == list:
                    temp = None
                    for j in range(len(logits[i+1])):
                        if temp is None:
                            temp = tf.math.reduce_mean(tf.keras.losses.mean_squared_error(logits[i+1][j], teacher_logits[i+1][j]))
                        else:
                            temp += tf.math.reduce_mean(tf.keras.losses.mean_squared_error(logits[i+1][j], teacher_logits[i+1][j]))
                    temp /= len(logits[i+1])
                else:
                    temp = tf.math.reduce_mean(tf.keras.losses.mean_squared_error(logits[i+1], teacher_logits[i+1]))

                if loss is None:
                    loss = temp
                else:
                    if loss.dtype != temp.dtype:
                        temp = tf.cast(temp, loss.dtype)
                    loss += temp

            if loss is None: # empty position case
                loss = tf.math.reduce_mean(tf.keras.losses.kl_divergence(logits[0], teacher_logits[0])) # model's output logit
            else:
                temp_ = tf.math.reduce_mean(tf.keras.losses.kl_divergence(logits[0], teacher_logits[0])) # model's output logit
                if loss.dtype != temp_.dtype:
                    temp_ = tf.cast(temp_, loss.dtype)
                loss += temp_
            if y is not None:
                temp_ = tf.math.reduce_mean(tf.keras.losses.categorical_crossentropy(logits[0], y))
                if loss.dtype != temp_.dtype:
                    temp_ = tf.cast(temp_, loss.dtype)    
                loss += temp_
        else:
            assert y is not None
            loss = tf.math.reduce_mean(tf.keras.losses.categorical_crossentropy(logits[0], y))

    if ret_last_tensor:
        return tape, loss, logits[-1]
    else:
        return tape, loss


def iteration_based_train(dataset, model, model_handler, max_iters, lr_mode=0, teacher=None, with_label=True, with_distillation=True, callback_before_update=None, stopping_callback=None, augment=True, n_classes=100, eval_steps=-1, validate_func=None):

    from nncompress.backend.tensorflow_ import SimplePruningGate
    from nncompress.backend.tensorflow_.transformation.pruning_parser import StopGradientLayer
    from utils import optimizer_factory
    custom_object_scope = {
        "SimplePruningGate":SimplePruningGate, "StopGradientLayer":StopGradientLayer, "HvdMovingAverage":optimizer_factory.HvdMovingAverage
    }   
    batch_size = model_handler.get_batch_size(dataset)
    model = add_augmentation(model, model_handler.width, train_batch_size=batch_size, do_mixup=True, do_cutmix=True, custom_objects=custom_object_scope)

    (train_data_generator, valid_data_generator, test_data_generator), (iters, iters_val) = load_dataset(dataset, model_handler, training_augment=augment, n_classes=n_classes)

    global_step = 0
    callbacks_ = model_handler.get_callbacks(iters)
    optimizer = model_handler.get_optimizer(lr_mode)

    epoch = 0
    first_batch = True
    with tqdm(total=max_iters // hvd.size(), ncols=120, disable=hvd.rank() != 0) as pbar:
        while global_step < max_iters // hvd.size(): 

            # start with new epoch.
            done = False
            idx = 0
            for X, y in train_data_generator:
                idx += 1
                y = tf.convert_to_tensor(y, dtype=tf.float32)
                if teacher is not None:
                    teacher_logits = teacher(X)
                    if type(teacher_logits) != list:
                        teacher_logits = [teacher_logits]
                else:
                    teacher_logits = None

                if callback_before_update is not None:
                    ret = callback_before_update(idx, global_step, X, model, teacher_logits, y, pbar)
                else:
                    ret = None

                if with_label:
                    if with_distillation:
                        tape, loss = train_step(X, model, teacher_logits, y)
                    else:
                        tape, loss = train_step(X, model, None, y)
                else:
                    tape, loss = train_step(X, model, teacher_logits, None)

                tape = hvd.DistributedGradientTape(tape)

                gradients = tape.gradient(loss, model.trainable_variables)
                optimizer.apply_gradients(zip(gradients, model.trainable_variables))

                global_step += 1
                if ret is not None and ret != 0:
                    pbar.update(ret)
                else:
                    pbar.update(1)

                if eval_steps != -1 and global_step % eval_steps == 0 and validate_func is not None:
                    val = validate_func()
                    if hvd.rank() == 0:
                        print("Global Steps %d: %f" % (global_step, val))
                        logging.info("Global Steps %d: %f" % (global_step, val))

                if first_batch:
                    hvd.broadcast_variables(model.variables, root_rank=0)
                    hvd.broadcast_variables(optimizer.variables(), root_rank=0)
                    first_batch = False

                if stopping_callback is not None and stopping_callback(idx, global_step):
                    done = True
                    break
                else:
                    if global_step >= max_iters // hvd.size():
                        done = True
                        break
                    else:
                        done = False
            if done:
                break
            else:
                train_data_generator.on_epoch_end()

            #epoch += 1
            #if validate_func is not None:
            #    print("Epoch %d: %f" % (epoch, validate_func()))
