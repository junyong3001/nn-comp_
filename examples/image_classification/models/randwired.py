import tensorflow as tf
import numpy as np
from tensorflow import keras
from tensorflow.keras.callbacks import Callback, EarlyStopping, ReduceLROnPlateau
import albumentations as albu
from .randwired_model import randwired_cifar, WeightedSum, randwired_cct
import cv2

def get_batch_size(dataset):
    if dataset != "cifar100":
        return 32
    else:
        return 128

def get_shape(dataset):
    if dataset != "cifar100":
        height = 64
        width = 64
    else:
        height = 32
        width = 32
    return (height, width, 3) # network input

def get_name():
    return "randwired"

def preprocess_func(img, shape):
    img = img.astype(np.float32)/255.
    img = cv2.resize(img, shape, interpolation=cv2.INTER_CUBIC)
    return img

def get_custom_objects():
    return {"WeightedSum":WeightedSum}

def get_model(dataset, n_classes=100):
    model = randwired_cifar(input_shape=get_shape(dataset), num_classes=n_classes)
    return model

def get_train_epochs():
    return 300

initial_lr = 0.01
def compile(model, run_eagerly=False):
    sgd = tf.keras.optimizers.SGD(lr=initial_lr, momentum=0.9, nesterov=True)
    model.compile(optimizer=sgd, loss='categorical_crossentropy', metrics=['accuracy'], run_eagerly=run_eagerly)

def lr_scheduler(epoch, lr):
    if epoch == 150:
        lr = lr * 0.1
    elif epoch == 200:
        lr = lr * 0.1
    print(lr)
    return lr

def get_callbacks(nsteps):
    #reducing learning rate on plateau
    #rlrop = ReduceLROnPlateau(monitor='val_loss', mode='min', patience= 5, factor= 0.5, min_lr= 1e-6, verbose=1)
    #return [rlrop]
    return [tf.keras.callbacks.LearningRateScheduler(lr_scheduler)]
