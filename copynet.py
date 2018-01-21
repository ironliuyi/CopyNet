import collections

import tensorflow as tf
from tensorflow.python.util import nest
from tensorflow.contrib.framework.python.framework import tensor_util

class CopyNetWrapperState(
    collections.namedtuple("CopyNetWrapperState", ("cell_state", "last_ids", "prob_c"))):

    def clone(self, **kwargs):
        def with_same_shape(old, new):
            """Check and set new tensor's shape."""
            if isinstance(old, tf.Tensor) and isinstance(new, tf.Tensor):
                return tensor_util.with_same_shape(old, new)
            return new

        return nest.map_structure(
            with_same_shape,
            self,
            super(CopyNetWrapperState, self)._replace(**kwargs))

class CopyNetWrapper(tf.nn.rnn_cell.RNNCell):

    def __init__(self, cell, encoder_states, encoder_input_ids, encoder_vocab_size,
            decoder_vocab_size, encoder_state_size=None, initial_cell_state=None, name=None):
        super(CopyNetWrapper, self).__init__(name=name)
        self._cell = cell
        self._encoder_vocab_size = encoder_vocab_size
        self._decoder_vocab_size = decoder_vocab_size

        self._encoder_input_ids = encoder_input_ids
        self._encoder_states = encoder_states
        if encoder_state_size is None:
            encoder_state_size = self._encoder_states.shape[-1].value
            if encoder_state_size is None:
                raise ValueError("encoder_state_size must be set if we can't infer encoder_states last dimension size.")
        self._encoder_state_size = encoder_state_size

        self._initial_cell_state = initial_cell_state
        self._copy_weight = tf.get_variable('CopyWeight', [self._encoder_state_size , self._cell.output_size])
        self._projection = tf.layers.Dense(self._decoder_vocab_size, use_bias=False, name="OutputProjection")


    def __call__(self, inputs, state, scope=None):
        if not isinstance(state, CopyNetWrapperState):
            raise TypeError("Expected state to be instance of CopyNetWrapperState. "
                      "Received type %s instead."  % type(state))
        last_ids = state.last_ids
        prob_c = state.prob_c
        cell_state = state.cell_state

        mask = tf.cast(tf.equal(tf.expand_dims(last_ids, 1),  self._encoder_input_ids), tf.float32)
        mask_sum = tf.reduce_sum(mask, axis=1)
        mask = tf.where(tf.less(mask_sum, 1e-7), mask, mask / tf.expand_dims(mask_sum, 1))
        rou = mask * prob_c
        selective_read = tf.reduce_sum(self._encoder_states * tf.expand_dims(rou, 2), 1)
        inputs = tf.concat([inputs, selective_read], 1)

        outputs, cell_state = self._cell(inputs, cell_state, scope)
        generate_score = self._projection(outputs)

        copy_score = tf.tensordot(self._encoder_states, self._copy_weight, [[2], [0]])
        copy_score = tf.nn.tanh(copy_score)

        #copy_score = tf.matmul(copy_score, tf.expand_dims(state, 2))
        copy_score = tf.reduce_sum(copy_score * tf.expand_dims(outputs, 1), 2)

        encoder_input_mask = tf.one_hot(self._encoder_input_ids, self._encoder_vocab_size)
        expanded_copy_score = tf.reduce_sum(encoder_input_mask * tf.expand_dims(copy_score, 2), 2)
        prob_g = generate_score
        prob_c = expanded_copy_score
#        mixed_score = tf.concat([generate_score, expanded_copy_score], 1)
#        probs = tf.nn.softmax(mixed_score)
#        prob_g = probs[:, :self._decoder_vocab_size]
#        prob_c = probs[:, self._decoder_vocab_size:]
        outputs = prob_g + tf.reduce_sum(encoder_input_mask * tf.expand_dims(prob_c,  2), 1)
        last_ids = tf.argmax(outputs, axis=-1, output_type=tf.int32)
        #prob_c.set_shape([None, self._encoder_state_size])
        state = CopyNetWrapperState(cell_state=cell_state, last_ids=last_ids, prob_c=prob_c)
        return outputs, state

    @property
    def state_size(self):
        """size(s) of state(s) used by this cell.

            It can be represented by an Integer, a TensorShape or a tuple of Integers
            or TensorShapes.
        """
        return CopyNetWrapperState(cell_state=self._cell.state_size, last_ids=tf.TensorShape([]),
            prob_c = self._encoder_state_size)

    @property
    def output_size(self):
        """Integer or TensorShape: size of outputs produced by this cell."""
        return self._decoder_vocab_size

    def zero_state(self, batch_size, dtype):
        with tf.name_scope(type(self).__name__ + "ZeroState", values=[batch_size]):
            if self._initial_cell_state is not None:
                cell_state = self._initial_cell_state
            else:
                cell_state = self._cell.zero_state(batch_size, dtype)
            last_ids = tf.zeros([batch_size], tf.int32) - 1
            prob_c = tf.zeros([batch_size, tf.shape(self._encoder_states)[1]], tf.float32)
            return CopyNetWrapperState(cell_state=cell_state, last_ids=last_ids, prob_c=prob_c)