#! -*- coding: utf-8 -*-
# RoBERTa预训练脚本，多GPU版/TPU版本

import os, re
os.environ['TF_KERAS'] = '1'  # 必须使用tf.keras

import tensorflow as tf
from data_utils import TrainingDataset
from bert4keras.bert import build_bert_model
from bert4keras.backend import keras, K
from bert4keras.optimizers import Adam
from bert4keras.optimizers import extend_with_weight_decay
from bert4keras.optimizers import extend_with_layer_adaptation
from bert4keras.optimizers import extend_with_piecewise_linear_lr
from bert4keras.optimizers import extend_with_gradient_accumulation

# 语料路径和模型保存路径
# 如果是TPU训练，那么语料必须存放在Google Cloud Storage上面，
# 路径必须以gs://开通；如果是GPU训练，改为普通路径即可。
best_model_saved_path = 'gs://xxxx/bert4keras/saved_model/best/bert_model.ckpt'
latest_model_saved_path = 'gs://xxxx/bert4keras/saved_model/latest/bert_model.ckpt'
corpus_paths = [
    'gs://xxxx/bert4keras/corpus/corpus.%s.tfrecord' % i
    for i in range(10)
]

# 其他配置
sequence_length = 512
batch_size = 4096
config_path = '/home/spaces_ac_cn/chinese_L-12_H-768_A-12/bert_config.json'
checkpoint_path = '/home/spaces_ac_cn/chinese_L-12_H-768_A-12/bert_model.ckpt' # 如果从零训练，就设为None
learning_rate = 0.00176
weight_decay_rate = 0.01
num_warmup_steps = 3125
num_train_steps = 125000
steps_per_epoch = 2000
grad_accum_steps = 16 # 大于1即表明使用梯度累积
epochs = num_train_steps * grad_accum_steps // steps_per_epoch
exclude_from_weight_decay = ['Norm', 'bias']
tpu_address = 'grpc://xxx.xxx.xxx.xxx:8470' # 如果用多GPU跑，直接设为None
which_optimizer = 'lamb'  # adam 或 lamb，均自带weight decay
lr_schedule = {
    num_warmup_steps * grad_accum_steps: 1.,
    num_train_steps * grad_accum_steps: 0.,
}

# 准备变量
Input = keras.layers.Input
Lambda = keras.layers.Lambda
Model = keras.models.Model

# 读取数据集，构建数据张量
dataset = TrainingDataset.load_tfrecord(
    record_names=corpus_paths,
    sequence_length=sequence_length,
    batch_size=batch_size // grad_accum_steps,
)


def build_train_bert_model():
    """构建训练模型，通用于TPU/GPU
    注意全程要用keras标准的层写法，一些比较灵活的“移花接木”式的
    写法可能会在TPU上训练失败。此外，要注意的是TPU并非支持所有
    tensorflow算子，尤其不支持动态（变长）算子，因此编写相应运算
    时要格外留意。
    """
    bert = build_bert_model(config_path, with_mlm='linear', return_keras_model=False)
    bert_model = bert.model
    proba = bert_model.output

    # 辅助输入
    token_ids = Input(shape=(None, ), dtype='int64', name='token_ids') # 目标id
    is_masked = Input(shape=(None, ), dtype='bool', name='is_masked') # mask标记

    def mlm_loss(inputs):
        """计算loss的函数，需要封装为一个层
        """
        y_true, y_pred, is_masked = inputs
        is_masked = K.cast(is_masked, K.floatx())
        loss = K.sparse_categorical_crossentropy(y_true, y_pred, from_logits=True)
        loss = K.sum(loss * is_masked) / (K.sum(is_masked) + K.epsilon())
        return loss

    def mlm_acc(inputs):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred, is_masked = inputs
        is_masked = K.cast(is_masked, K.floatx())
        y_true = K.cast(y_true, K.floatx())
        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.sum(acc * is_masked) / (K.sum(is_masked) + K.epsilon())
        return acc

    loss = Lambda(mlm_loss, name='mlm_loss')([token_ids, proba, is_masked])
    acc = Lambda(mlm_acc, name='mlm_acc')([token_ids, proba, is_masked])

    train_model = Model(bert_model.inputs + [token_ids, is_masked], [loss, acc])

    # 优化器
    optimizer = extend_with_weight_decay(Adam)
    if which_optimizer == 'lamb':
        optimizer = extend_with_layer_adaptation(optimizer)
    optimizer = extend_with_piecewise_linear_lr(optimizer)
    optimizer_params = {
        'learning_rate': learning_rate,
        'lr_schedule': lr_schedule,
        'weight_decay_rate': weight_decay_rate,
        'exclude_from_weight_decay': exclude_from_weight_decay,
        'bias_correction': False,
    }
    if grad_accum_steps > 1:
        optimizer = extend_with_gradient_accumulation(optimizer)
        optimizer_params['grad_accum_steps'] = grad_accum_steps
    optimizer = optimizer(**optimizer_params)

    # 模型定型
    train_model.compile(
        loss={
            'mlm_loss': lambda y_true, y_pred: y_pred,
            'mlm_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
        },
        optimizer=optimizer,
    )

    # 如果传入权重，则加载。注：须在此处加载，才保证不报错。
    if checkpoint_path is not None:
        bert.load_weights_from_checkpoint(checkpoint_path)

    return train_model


if tpu_address is None:
    # 单机多卡模式（多机多卡也类似，但需要硬软件配合，请参考https://tf.wiki）
    strategy = tf.distribute.MirroredStrategy()
else:
    # TPU模式
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=tpu_address)
    tf.config.experimental_connect_to_host(resolver.master())
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.experimental.TPUStrategy(resolver)

with strategy.scope():
    train_model = build_train_bert_model()
    train_model.summary()


class ModelCheckpoint(keras.callbacks.ModelCheckpoint):
    """除了保存最优模型外，每个epoch自动保存最新模型
    """
    def on_epoch_end(self, epoch, logs=None):
        super(ModelCheckpoint, self).on_epoch_end(epoch, logs)
        self.model.save_weights(latest_model_saved_path, overwrite=True)


# 保存模型
checkpoint = ModelCheckpoint(
    filepath=best_model_saved_path,
    monitor='mlm_loss_loss',
    save_weights_only=True,
    save_best_only=True,
)
# 记录日志
csv_logger = keras.callbacks.CSVLogger('training.log')

# 模型训练
train_model.fit(
    dataset,
    steps_per_epoch=steps_per_epoch,
    epochs=epochs,
    callbacks=[checkpoint, csv_logger],
)
