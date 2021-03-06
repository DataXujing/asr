#coding=utf-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import logging
import numpy as np
import tensorflow as tf

from tensorflow.contrib.rnn import LSTMCell, GRUCell, MultiRNNCell, DropoutWrapper

class Attention_sum_reader(object):
    def __init__(self, name, d_len, q_len, A_len, lr_init, lr_decay, embedding_matrix, hidden_size, num_layers):
        self._name = name
        self._d_len = d_len
        self._q_len = q_len
        self._A_len = A_len
        self._lr_init = lr_init
        self._lr_decay = lr_decay
        #self._embedding_matrix = tf.Variable(embedding_matrix, dtype=tf.float32)
        self._embedding_matrix = embedding_matrix
        self._hidden_size = hidden_size
        self._num_layers = num_layers

        self._d_input = tf.placeholder(dtype=tf.int32, shape=(None, d_len), name='d_input')
        self._q_input = tf.placeholder(dtype=tf.int32, shape=(None, q_len), name='q_input')
        self._context_mask = tf.placeholder(dtype=tf.int8, shape=(None, d_len), name='context_mask')
        self._ca = tf.placeholder(dtype=tf.int32, shape=(None, A_len), name='ca')
        self._y = tf.placeholder(dtype=tf.int32, shape=(None), name='y')

        self._build_network()

        self._saver = tf.train.Saver()

    def train(self, sess, provider, save_dir, save_period, model_path=None):
        sess.run(tf.global_variables_initializer())

        if model_path:
            logging.info('[restore] {}'.format(model_path))
            self._saver.restore(sess, model_path)

        losses = []
        predictions = []
        for data in provider:
            d_input, q_input, context_mask, ca, y = data
            _, loss, prediction = sess.run(
                    [self._train_op, self._loss, self._prediction], 
                    feed_dict={self._d_input: d_input, self._q_input: q_input, self._context_mask: context_mask,
                    self._ca: ca, self._y: y})
            losses.append(loss)
            predictions.append(prediction / len(d_input))

            step = sess.run(self._global_step)

            if step % 100 == 0:
                logging.info('[Train] step: {}, loss: {}, prediction: {}, lr: {}'.format(
                            step,
                            np.sum(losses) / len(losses),
                            np.sum(predictions) / len(predictions),
                            sess.run(self._lr)))
                losses = []
                predictions = []

            if step % save_period == 0 and step > 0:
                save_path = os.path.join(save_dir, self._name)
                logging.info('[Save] {} {}'.format(save_path, step))
                self._saver.save(sess, save_path, global_step=self._global_step)


    def test(self, sess, provider, model_path):
        logging.info('[restore] {}'.format(model_path))
        self._saver.restore(sess, model_path)
        
        q_num = 0.0
        p_num = 0.0
        for (i, data) in enumerate(provider):
            d_input, q_input, context_mask, ca, y = data
            prediction = sess.run(
                    self._prediction,
                    feed_dict={self._d_input: d_input, self._q_input: q_input, self._context_mask: context_mask,
                    self._ca: ca, self._y: y})
            
            q_num += len(d_input)
            p_num += prediction
            
            if i % 50 == 0:
                logging.info('[test] q_num: {}, p_num: {}, {}'.format(q_num, p_num, float(p_num)/q_num))

        logging.info('[test] q_num: {}, p_num: {}, {}'.format(q_num, p_num, float(p_num)/q_num))

    def _RNNCell(self):
        cell = GRUCell(self._hidden_size)
        #cell = LSTMCell(self._hidden_size)
        return DropoutWrapper(cell, input_keep_prob=0.8, output_keep_prob=0.8)
        #return cell
    
    def _Optimizer(self, global_step):
        self._lr = tf.train.exponential_decay(self._lr_init, global_step, self._lr_decay, 0.5, staircase=True)

        #return tf.train.GradientDescentOptimizer(self._lr)
        return tf.train.AdamOptimizer(self._lr)

    def _build_network(self):
        with tf.variable_scope('q_encoder'):
            q_embed = tf.nn.embedding_lookup(self._embedding_matrix, self._q_input)
            q_lens = tf.reduce_sum(tf.sign(tf.abs(self._q_input)), 1)
            outputs, final_states = tf.nn.bidirectional_dynamic_rnn(
                    cell_bw=self._RNNCell(), cell_fw=self._RNNCell(),
                    inputs=q_embed, dtype=tf.float32, sequence_length=q_lens)
            q_encode = tf.concat([final_states[0], final_states[1]], axis=-1)
            #q_encode = tf.concat([final_states[0][-1][1], final_states[1][-1][1]], axis=-1)

            # [batch_size, hidden_size * 2]
            logging.info('q_encode shape {}'.format(q_encode.get_shape()))
            logging.info('q_encode shape {}'.format(final_states[0][-1][0].get_shape()))

        with tf.variable_scope('d_encoder'):
            d_embed = tf.nn.embedding_lookup(self._embedding_matrix, self._d_input)
            d_lens = tf.reduce_sum(tf.sign(tf.abs(self._d_input)), 1)
            outputs, final_states = tf.nn.bidirectional_dynamic_rnn(
                    cell_bw=self._RNNCell(), cell_fw=self._RNNCell(),
                    inputs=d_embed, dtype=tf.float32, sequence_length=d_lens)
            d_encode = tf.concat(outputs, axis=-1)

            # [batch_size, d_len, hidden_size * 2]
            logging.info('d_encode shape {}'.format(d_encode.get_shape()))

        with tf.variable_scope('dot_sum'):
            def reduce_attention_sum(data):
                at, d, ca = data
                def reduce_attention_sum_by_ans(aid):
                    return tf.reduce_sum(tf.multiply(at, tf.cast(tf.equal(d, aid), tf.float32)))
                return tf.map_fn(reduce_attention_sum_by_ans, ca, dtype=tf.float32)

            attention_value = tf.map_fn(
                    lambda v: tf.reduce_sum(tf.multiply(v[0], v[1]), -1),
                    (q_encode, d_encode),
                    dtype=tf.float32)
            attention_value_masked = tf.multiply(attention_value, tf.cast(self._context_mask, tf.float32))
            attention_value_softmax = tf.nn.softmax(attention_value_masked)
            self._attention_sum = tf.map_fn(reduce_attention_sum, 
                    (attention_value_softmax, self._d_input, self._ca), dtype=tf.float32)

            # [batch_size, A_len]
            logging.info('attention_sum shape {}'.format(self._attention_sum.get_shape()))

        with tf.variable_scope('prediction'):
            self._prediction = tf.reduce_sum(tf.cast(
                        tf.equal(tf.cast(self._y, dtype=tf.int64), tf.argmax(self._attention_sum, 1)), tf.float32))

        with tf.variable_scope('loss'):
            label = tf.Variable([1., 0., 0., 0., 0., 0., 0., 0., 0., 0.], dtype=tf.float32, trainable=False)
            #self._output = self._attention_sum / tf.reduce_sum(self._attention_sum, -1, keep_dims=True)
            self._loss = tf.reduce_mean(-tf.log(tf.reduce_sum(self._attention_sum * label, -1)))

        with tf.variable_scope('train'):
            self._global_step = tf.contrib.framework.get_or_create_global_step()
            optimizer = self._Optimizer(self._global_step)
            self._train_op = optimizer.minimize(self._loss, global_step=self._global_step)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    embedded = tf.zeros((1000, 100), dtype=tf.float32)
    Attention_sum_reader(name='miao', d_len=600, q_len=60, A_len=10, lr=0.1, embedding_matrix=embedded, hidden_size=128, num_layers=2)
