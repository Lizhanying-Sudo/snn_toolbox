# -*- coding: utf-8 -*-
"""
Building and running spiking neural networks using Intel's Loihi platform.
@author: rbodo
"""

from __future__ import division, absolute_import
from __future__ import print_function, unicode_literals

import warnings

import numpy as np
from future import standard_library

from snntoolbox.simulation.utils import AbstractSNN
from snntoolbox.utils.utils import to_integer
from snntoolbox.simulation.plotting import plot_probe

standard_library.install_aliases()


class SNN(AbstractSNN):
    """Class to hold the compiled spiking neural network.

    Represents the compiled spiking neural network, ready for testing in a
    spiking simulator.

    Attributes
    ----------

    layers: list[pyNN.Population]
        Each entry represents a layer, i.e. a population of neurons, in form of
        pyNN ``Population`` objects.

    """

    def __init__(self, config, queue=None):

        AbstractSNN.__init__(self, config, queue)

        self.layers = []
        self.probes = []
        self.probe_idx_map = {}
        self.net = self.sim.NxNet()
        self.board = None
        self.core_counter = 0
        self.num_cores_per_layer = \
            eval(self.config.get('loihi', 'num_cores_per_layer'))
        self.num_weight_bits = eval(self.config.get(
            'loihi', 'connection_kwargs'))['numWeightBits']
        self.threshold_scales = None
        partition = self.config.get('loihi', 'partition', fallback='')
        self.partition = None if partition == '' else partition

    @property
    def is_parallelizable(self):
        return False

    def add_input_layer(self, input_shape):

        if self._poisson_input:
            raise NotImplementedError

        num_neurons = np.prod(input_shape[1:], dtype=np.int).item()

        compartment_kwargs = eval(self.config.get('loihi',
                                                  'compartment_kwargs'))
        scale = self.threshold_scales[self.parsed_model.layers[0].name]
        compartment_kwargs['vThMant'] *= 2 ** scale
        prototypes, prototype_map = self.partition_layer(num_neurons,
                                                         compartment_kwargs)

        self.layers.append(self.net.createCompartmentGroup(
            self.parsed_model.layers[0].name, num_neurons, prototypes,
            prototype_map))

    def add_layer(self, layer):

        if 'Flatten' in layer.__class__.__name__:
            self.flatten_shapes.append(
                (layer.name, get_shape_from_label(self.layers[-1].name)))
            return

        num_neurons = np.prod(layer.output_shape[1:], dtype=np.int).item()

        compartment_kwargs = eval(self.config.get('loihi',
                                                  'compartment_kwargs'))
        scale = self.threshold_scales[layer.name]
        compartment_kwargs['vThMant'] *= 2 ** scale
        prototypes, prototype_map = self.partition_layer(num_neurons,
                                                         compartment_kwargs)

        self.layers.append(self.net.createCompartmentGroup(
            layer.name, num_neurons, prototypes, prototype_map))

    def build_dense(self, layer):
        """

        Parameters
        ----------
        layer : keras.layers.Dense

        Returns
        -------

        """

        if layer.activation.__name__ == 'softmax':
            warnings.warn("Activation 'softmax' not implemented. Using 'relu' "
                          "activation instead.", RuntimeWarning)

        weights, biases = layer.get_weights()

        if len(self.flatten_shapes):
            _, shape = self.flatten_shapes.pop()
            weights = fix_flatten(weights, shape, self.data_format)

        weights, biases = to_integer(weights, biases, self.num_weight_bits)

        self.set_biases(biases)

        scale = self.threshold_scales[self.layers[-2].name]
        self.connect(weights, scale)

    def build_convolution(self, layer):
        from snntoolbox.simulation.utils import build_convolution

        transpose_kernel = \
            self.config.get('simulation', 'keras_backend') == 'tensorflow'

        connections, biases = build_convolution(layer, 0, transpose_kernel)

        shape = (np.prod(layer.input_shape[1:]),
                 np.prod(layer.output_shape[1:]))
        weights = connection_list_to_matrix(connections, shape)

        weights, biases = to_integer(weights, biases, self.num_weight_bits)

        self.set_biases(biases)

        scale = self.threshold_scales[self.layers[-2].name]
        self.connect(weights, scale)

    def build_pooling(self, layer):
        from snntoolbox.simulation.utils import build_pooling

        connections = build_pooling(layer, 0)

        weights = connection_list_to_matrix(connections, layer.output_shape)

        scale = self.threshold_scales[self.layers[-2].name]
        self.connect(weights, scale)

    def compile(self):

        vars_to_record = self.get_vars_to_record()

        for layer in self.layers:
            self.probes.append(layer.probe(vars_to_record))

        # The spikes of the last layer are recorded by default because they
        # contain the networks output (classification guess).
        if 'spikes' not in self.probe_idx_map.keys():
            vars_to_record.append(self.sim.ProbeParameter.SPIKE)
            self.probes[-1] = self.layers[-1].probe(vars_to_record)
            self.probe_idx_map['spikes'] = len(vars_to_record) - 1

        self.board = self.sim.N2Compiler().compile(self.net)

    def simulate(self, **kwargs):

        data = kwargs[str('x_b_l')]
        if self.data_format == 'channels_last' and data.ndim == 4:
            data = np.moveaxis(data, 3, 1)
        self.set_inputs(np.ravel(data))

        self.board.run(self._duration, partition=self.partition)

        print("\nCollecting results...")
        output_b_l_t = self.get_recorded_vars(self.layers)

        return output_b_l_t

    def reset(self, sample_idx):

        print("Resetting membrane potentials...")
        for layer in self.layers:
            for i, node_id in enumerate(layer.nodeIds):
                _, chip_id, core_id, cx_id, _, _ = \
                    self.net.resourceMap.compartment(node_id)
                core = self.board.n2Chips[chip_id].n2Cores[core_id]
                core.cxState[int(cx_id)].v = 0
                setattr(core.cxMetaState[int(cx_id // 4)],
                        'phase{}'.format(cx_id % 4), 2)
        print("Done.")

    def end_sim(self):

        self.board.disconnect()

    def save(self, path, filename):

        pass

    def load(self, path, filename):

        raise NotImplementedError

    def init_cells(self):

        pass

    def set_biases(self, biases):
        """Set biases."""

        if not np.any(biases):
            return

        self.layers[-1].setState('biasMant', biases.astype(int))
        # It should not be necessary to set the biasExp here, because we set it
        # from the config file already. But even though we can read the correct
        # value via getState, it does not have any effect on the neuron. Only
        # if we set it explicitly again here:
        self.layers[-1].setState('biasExp', eval(self.config.get(
            'loihi', 'compartment_kwargs'))['biasExp'])

    def get_vars_to_record(self):
        """Get variables to record during simulation.

        Returns
        -------

        vars_to_record: list[str]
            The names of variables to record during simulation.
        """

        vars_to_record = []
        if any({'spiketrains', 'spikerates', 'correlation', 'spikecounts',
                'hist_spikerates_activations'} & self._plot_keys) \
                or 'spiketrains_n_b_l_t' in self._log_keys:
            vars_to_record.append(self.sim.ProbeParameter.SPIKE)
            self.probe_idx_map['spikes'] = len(vars_to_record) - 1

        if 'mem_n_b_l_t' in self._log_keys or 'v_mem' in self._plot_keys:
            vars_to_record.append(self.sim.ProbeParameter.COMPARTMENT_VOLTAGE)
            self.probe_idx_map['v_mem'] = len(vars_to_record) - 1

        return vars_to_record

    def get_spiketrains(self, **kwargs):
        j = self._spiketrains_container_counter
        if self.spiketrains_n_b_l_t is None \
                or j >= len(self.spiketrains_n_b_l_t):
            return None

        shape = self.spiketrains_n_b_l_t[j][0].shape

        # Outer for-loop that calls this function starts with
        # 'monitor_index' = 0, but this is reserved for the input and handled
        # by `get_spiketrains_input()`.
        i = len(self.layers) - 1 if kwargs[str('monitor_index')] == -1 else \
            kwargs[str('monitor_index')] + 1
        idx = self.probe_idx_map['spikes']
        spiketrains_flat = self.probes[i][idx].data[:, -self._num_timesteps:]
        spiketrains_b_l_t = self.reshape_flattened_spiketrains(
            spiketrains_flat, shape, False)
        return spiketrains_b_l_t

    def get_spiketrains_input(self):
        shape = list(self.parsed_model.input_shape) + [self._num_timesteps]
        idx = self.probe_idx_map['spikes']
        spiketrains_flat = self.probes[0][idx].data[:, -self._num_timesteps:]
        spiketrains_b_l_t = self.reshape_flattened_spiketrains(
            spiketrains_flat, shape, False)
        return spiketrains_b_l_t

    def get_spiketrains_output(self):
        shape = [self.batch_size, self.num_classes, self._num_timesteps]
        idx = self.probe_idx_map['spikes']
        spiketrains_flat = self.probes[-1][idx].data[:, -self._num_timesteps:]
        spiketrains_b_l_t = self.reshape_flattened_spiketrains(
            spiketrains_flat, shape, False)
        return spiketrains_b_l_t

    def get_vmem(self, **kwargs):
        i = kwargs[str('monitor_index')]
        if 'v_mem' in self.probe_idx_map.keys():
            idx = self.probe_idx_map['v_mem']
            # Need to skip input layer because the toolbox does not expect it
            # to record the membrane potentials.
            if i == 0:
                plot_probe(self.probes[i][idx],
                           self.config.get('paths', 'log_dir_of_current_run'),
                           'v_input.png')
            else:
                return self.probes[i][idx].data[:, -self._num_timesteps:]

    def set_spiketrain_stats_input(self):
        AbstractSNN.set_spiketrain_stats_input(self)

    def partition_layer(self, num_neurons, compartment_kwargs):
        num_cores = self.num_cores_per_layer.pop(0)
        num_neurons_per_core = \
            np.ones(num_cores, int) * int(num_neurons / num_cores)
        num_neurons_per_core[:num_neurons % num_cores] += 1
        core_id_map = np.repeat(np.arange(self.core_counter,
                                          self.core_counter + num_cores),
                                num_neurons_per_core)
        prototypes = []
        for core_id in core_id_map:
            compartment_kwargs['logicalCoreId'] = core_id
            prototypes.append(
                self.sim.CompartmentPrototype(**compartment_kwargs))
        prototype_map = core_id_map - self.core_counter
        self.core_counter += num_cores

        return prototypes, list(prototype_map)

    def connect(self, weights, scale):

        # Even though we already converted the weights to integers during
        # parsing, they will have become float type again after compiling the
        # internal Keras model. The transpose is necessary for Loihi
        # convention.
        weights = weights.transpose().astype(int)

        connection_kwargs = eval(self.config.get('loihi', 'connection_kwargs'))

        connection_kwargs['weightExponent'] += scale

        assert connection_kwargs['compressionMode'] == 0, \
            "Compression mode must be SPARSE when splitting the weight " \
            "matrix into excitatory and inhibitory connections " \
            "(signMode 2, 3) via the connectionMask argument. (DENSE " \
            "compression mode does not properly handle holes in the weight " \
            "matrix."

        connection_kwargs['signMode'] = 2
        self.net.createConnectionGroup(
            src=self.layers[-2], dst=self.layers[-1],
            prototype=self.sim.ConnectionPrototype(**connection_kwargs),
            connectionMask=weights > 0, weight=weights)

        connection_kwargs['signMode'] = 3
        self.net.createConnectionGroup(
            src=self.layers[-2], dst=self.layers[-1],
            prototype=self.sim.ConnectionPrototype(**connection_kwargs),
            connectionMask=weights < 0, weight=weights)

    def set_inputs(self, inputs):
        # Normalize inputs and scale up to 8 bit.
        inputs = (inputs / np.max(inputs) * 2 ** 8).astype(int)
        for i, node_id in enumerate(self.layers[0].nodeIds):
            _, chip_id, core_id, cx_id, _, _ = \
                self.net.resourceMap.compartment(node_id)
            self.board.n2Chips[chip_id].n2Cores[core_id].cxCfg[
                int(cx_id)].bias = int(inputs[i])

    def preprocessing(self, **kwargs):
        print("Normalizing thresholds.")
        from snntoolbox.conversion.utils import normalize_loihi_network
        self.threshold_scales = normalize_loihi_network(self.parsed_model,
                                                        self.config, **kwargs)


def get_shape_from_label(label):
    """
    Extract the output shape of a flattened pyNN layer from the layer name
    generated during parsing.

    Parameters
    ----------

    label: str
        Layer name containing shape information after a '_' separator.

    Returns
    -------

    : list
        The layer shape.

    Example
    -------
        >>> get_shape_from_label('02Conv2D_16x32x32')
        [16, 32, 32]

    """
    return [int(i) for i in label.split('_')[1].split('x')]


def fix_flatten(weights, layer_shape, data_format):
    print("Swapping data_format of Flatten layer.")
    if data_format == 'channels_last':
        y_in, x_in, f_in = layer_shape
    else:
        f_in, y_in, x_in = layer_shape
    i_new = []
    for i in range(len(weights)):  # Loop over input neurons
        # Sweep across channel axis of feature map. Assumes that each
        # consecutive input neuron lies in a different channel. This is
        # the case for channels_last, but not for channels_first.
        f = i % f_in
        # Sweep across height of feature map. Increase y by one if all
        # rows along the channel axis were seen.
        y = i // (f_in * x_in)
        # Sweep across width of feature map.
        x = (i // f_in) % x_in
        i_new.append(f * x_in * y_in + y * x_in + x)

    return weights[np.argsort(i_new)]  # Move rows to new i's.


def connection_list_to_matrix(connection_list, shape):

    weights = np.zeros(shape)

    for y, x, w, _ in connection_list:
        weights[y, x] = w

    return weights