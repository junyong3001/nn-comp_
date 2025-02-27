
import logging
import types
import json
from collections import OrderedDict
import math
import copy
import random

import tensorflow as tf
from tensorflow import keras
import numpy as np
from numba import njit
from numpy import dot
from numpy.linalg import norm as npnorm

import horovod.tensorflow as hvd

from nncompress.backend.tensorflow_.transformation.pruning_parser import PruningNNParser, NNParser
from nncompress.backend.tensorflow_ import SimplePruningGate, DifferentiableGate
from nncompress.backend.tensorflow_.transformation import parse, inject, cut, unfold
from nncompress.backend.tensorflow_.transformation.pruning_parser import StopGradientLayer

from train import train_step

def find_all(model, Target):
    ret = []
    for layer in model.layers:
        if hasattr(layer, "layers"):
            ret += find_all(layer, Target)
        else:
            if layer.__class__ == Target:
                ret.append(layer)
    return ret

def compute_sparsity(groups, l2g, gmodel):
    sum_ = 0
    alive = 0
    for gidx, group in enumerate(groups):
        if type(group) == dict:
            # size, denominator
            items = []
            max_ = 0
            for key, val in group.items():
                if type(key) == str:
                    val = sorted(val, key=lambda x:x[0])
                    items.append((key, val))
                    for v in val:
                        if v[1] > max_:
                            max_ = v[1]
            sum_ += max_
            mask = np.zeros((max_,))
            for key, val in items:
                gate = gmodel.get_layer(l2g[key])
                for v in val:
                    mask[v[0]:v[1]] += gate.gates.numpy()
            mask = (mask >= 1.0).astype(np.float32)
            alive += np.sum(mask)
        else:
            g = group[0]
            sum_ += g.ngates
            alive += np.sum(g.gates.numpy())
    return 1.0 - alive / sum_

@njit
def find_min(cscore, gates, min_val, min_idx, gidx, ncol):
    for i in range(ncol):
        if (min_idx[0] == -1 or min_val > cscore[i]) and gates[i] == 1.0:
            min_val = cscore[i]
            min_idx = (gidx, i)
    assert min_idx[0] != -1
    return min_val, min_idx

def compute_act(layer, batch_size, pruning_input_gate=False, out_gate=None):

    if pruning_input_gate:
        if layer.__class__.__name__ == "Conv2D":
            w = layer.get_weights()[0].shape
            if out_gate is None:
                return batch_size * np.prod(list(w[0:2])+[w[3]])
            else:
                return batch_size * np.prod(list(w[0:2])+[np.sum(out_gate)])
        elif layer.__class__.__name__ == "SeparableConv2D":
            w1 = layer.get_weights()[0].shape
            w2 = layer.get_weights()[1].shape
            print(w1, w2)
            xxxxxxxxxxxx # later check.
            return batch_size * np.prod(list(w2[0:2])+[w2[3]])
        elif layer.__class__.__name__ == "Dense":
            w = layer.get_weights()[0].shape
            return batch_size * w[1]
        elif layer.__class__.__name__ == "MultiHeadAttention":
            w = layer.get_weights()[0].shape
            return batch_size * w[0] * 3
        else:
            return 0.0
    else: 
        if layer.__class__.__name__ == "Conv2D" or\
            (layer.__class__.__name__ == "DepthwiseConv2D" and not pruning_input_gate):
            # DepthwiseConv2D only contributes for the gate associated with its outputs.
            assert layer.groups == 1
            return batch_size * np.prod(layer.get_weights()[0].shape[0:3])
        elif layer.__class__.__name__ == "SeparableConv2D":
            return batch_size * np.prod(layer.get_weights()[0].shape[0:3]) + batch_size * np.prod(layer.get_weights()[1].shape[0:3])
        else:
            return 0.0

def flatten(group):
    ret = []
    if type(group) == str:
        return [group]
    else:
        for g in group:
            ret += flatten(g)
    return list(set(ret))

def extract_parents(p):
    ret = []
    if type(p) != frozenset and type(p[0]) == str:
        return [p[0]]
    else:
        for g in p:
            ret += extract_parents(g)
    return ret
   

def add_gates(model, custom_objects=None, avoid=None):

    model = unfold(model, custom_objects)
    parser = PruningNNParser(model, custom_objects=custom_objects, gate_class=SimplePruningGate)
    parser.parse()

    gmodel, gate_mapping = parser.inject(avoid=avoid, with_mapping=True, with_splits=True)
    #tf.keras.utils.plot_model(model, "ddd.png")
    #tf.keras.utils.plot_model(gmodel, "ggg.png")

    v = parser.traverse()
    torder = {
        name:idx
        for idx, (name, _) in enumerate(v)
    }

    def compare_key(x):
        id_ = x.name.split("_")[-1]
        if id_ == "gate":
            id_ = 0
        else:
            id_ = int(id_)
        return id_

    l2g = {}
    for layer, flow in gate_mapping:
        if gate_mapping[(layer, flow)][0] is None:
            continue
        l2g[layer] = gate_mapping[(layer, flow)][0]["config"]["name"]

    groups_ = parser.get_sharing_groups()
    ordered_groups = []
    for g in groups_:
        ### flatten
        g_flatten = flatten(g)

        g_ = sorted(list(g_flatten), key=lambda x: torder[x])
        #g_ = sorted(list(g), key=lambda x: torder[x])
        ordered_groups.append((g, torder[g_[0]]))
        ###
        #ordered_groups.append((g, torder[g_[0]]))
    ordered_groups = sorted(ordered_groups, key=lambda x: x[1])

    # ready for collecting
    for layer in gmodel.layers:
        if layer.__class__ == SimplePruningGate:
            layer.grad_holder = []
            layer.collecting = False

    return gmodel, model, l2g, ordered_groups, torder, parser, gate_mapping


def compute_positions(model, ordered_groups, torder, parser, position_mode, num_blocks, heuristic_positions=None):

    if num_blocks == -1: # heuristic block
        convs = [
           layer.name for layer in model.layers if "Conv2D" in layer.__class__.__name__
        ]

        blocks = [[]]
        current_id = 0
        for i, (g, idx) in enumerate(ordered_groups):

            trank = torder[heuristic_positions[current_id]]
            is_previous = False
            g_flatten = flatten(g)
            for layer in g_flatten:
                _trank = torder[layer]
                if _trank < trank:
                    is_previous = True
                    break
     
            if is_previous:
                blocks[current_id].append((g, heuristic_positions[current_id]))
            else:
                current_id += 1
                blocks.append([])
                blocks[current_id].append((g, heuristic_positions[current_id]))

        return heuristic_positions        

    else:
        convs = [
            layer.name for layer in model.layers if "Conv2D" in layer.__class__.__name__
        ]

        blocks = [[]]
        current_id = 0
        for i, (g, idx) in enumerate(ordered_groups):

            #des = parser.first_common_descendant(list(g), joints)
            g_flatten = flatten(g)
            des = parser.first_common_descendant(list(g_flatten), convs)
            blocks[current_id].append((g, des))
            if len(blocks[current_id]) >= len(ordered_groups) // num_blocks and current_id != num_blocks-1:
                current_id += 1
                blocks.append([])

    # compute positions
    convs = [
        layer.name for layer in model.layers if "Conv2D" in layer.__class__.__name__
    ]
    all_ = [
        layer.name for layer in model.layers
    ]

    all_acts_ = [
        layer.name for layer in model.layers if layer.__class__.__name__ in ["Activation", "ReLU", "Softmax"]
    ]

    if position_mode == 0:
        positions = all_acts_

    elif position_mode == 4: # 1x1 conv
        positions = []
        for c in convs:
            if gmodel.get_layer(c).kernel_size == (1,1):
                positions.append(c)
    elif position_mode == 1: # joints
        positions = []
        for b in blocks:
            if b[-1][1] is None:
                c = b[-1][0][0]
                act = parser.get_first_activation(b[-1][0][0]) # last layer.
            else:
                c = b[-1][1]
                act = parser.get_first_activation(b[-1][1])

            if act not in positions and act is not None:
                positions.append(act)
            else:
                positions.append(c)
        print(positions)

    elif position_mode == 2: # random
        positions = [
            all_acts_[int(random.random() * len(all_acts_))] for i in range(len(blocks))
        ]

    elif position_mode == 3: # cut
        positions  = []
        for b in blocks:
            g = b[-1][0]
            des = parser.first_common_descendant(list(g), all_acts_, False)

            if des not in positions:
                positions.append(des)
            """
            des_g = None
            for g_, idx in ordered_groups:
                if des in g_:
                    des_g = g_
                    break

            if des_g is not None:
                for l in des_g:
                    if l not in positions:
                        positions.append(l)
            else:
                if des not in positions:
                    positions.append(des) # maybe the last transforming layer.
            """

    elif position_mode == 5: # cut
        affecting_layers = parser.get_affecting_layers()

        cnt = {}
        for layer_ in affecting_layers:
            for a in affecting_layers[layer_]:
                if a[0] not in cnt:
                    cnt[a[0]] = 0
                cnt[a[0]] += 1

        node_list = [
            (key, value) for key, value in  cnt.items()
        ]
        node_list = sorted(node_list, key=lambda x: x[1])

        k = 5
        positions = [
            parser.get_first_activation(n) for n, _ in node_list[-1*k:]
        ]

    elif position_mode == 6: # cut
        affecting_layers = parser.get_affecting_layers()

        cnt = {
            layer_[0]:0 for layer_ in affecting_layers
        }
        for layer_ in affecting_layers:
            cnt[layer_[0]] = len(affecting_layers[layer_])

        node_list = [
            (key, value) for key, value in  cnt.items()
        ]
        node_list = sorted(node_list, key=lambda x: x[1])

        k = 5
        positions = [
            parser.get_first_activation(n) for n, _ in node_list[-1*k:]
        ]

    elif position_mode == 7:

        cons_ = [
            (c, torder[c]) for c in convs
        ]

        node_list = sorted(cons_, key=lambda x: x[1])

        k = 5
        positions = [
            n for n, _ in node_list[-1*k:]
        ]

    elif position_mode == 8:
        alll_ = [
            (c, torder[c]) for c in all_ if gmodel.get_layer(c).__class__.__name__ in ["Activation", "ReLU", "Softmax"]
        ]
        node_list = sorted(alll_, key=lambda x: x[1])

        k = 5
        positions = [
            n for n, _ in node_list[-1*k:]
        ]
        print(node_list)

    elif position_mode == 9:
        alll_ = [
            (c, torder[c]) for c in all_
        ]
        node_list = sorted(alll_, key=lambda x: x[1])

        k = 5
        positions = [
            n for n, _ in node_list[-1*k:]
        ]
        print(node_list)

    elif position_mode == 10: # cut
        positions  = []
        for b in blocks:
            g = b[-1][0]
            des = parser.first_common_descendant(list(g), convs, False)

            des_g = None
            for g_, idx in ordered_groups:
                if des in g_:
                    des_g = g_
                    break

            if des_g is not None:
                des = parser.first_common_descendant(list(des_g), all_acts_, False)
                if des not in positions:
                    positions.append(des)
            else:
                act = parser.get_first_activation(des)
                if act is None:
                    act = des
                if act not in positions:
                    positions.append(act) # maybe the last transforming layer.

    elif position_mode == 999:
        return []

    elif position_mode == 99: # k-path cover

        # get graph
        graph = parser._graph
        from overlayflow.cover import i_kpathcover
        positions_ = list(i_kpathcover(graph, k=7, directed=False))

        positions = set()
        for p in positions_:
            try:
                a = parser.get_first_activation(p)
                positions.add(a)
            except Exception as e:
                pass
        positions = list(positions)

    return positions


def compute_norm(parser, gate_mapping, gmodel, batch_size, targets, groups, inv_groups, l2g):
    norm = {
        t.name: 0.0
        for t in targets
    }
    parents = {}
    g2l = {}

    contributors = {}

    for l in l2g:
        if gmodel.get_layer(l).__class__.__name__ in ["Conv2D", "Dense", "MultiHeadAttention", "PatchingAndEmbedding"]:
            g2l[l2g[l]] = l

    affecting = parser.get_affecting_layers()
    for child, parents_ in affecting.items():
        if gmodel.get_layer(child[0]).__class__.__name__ not in ["Conv2D", "Dense", "MultiHeadAttention", "PacthingAndEmbedding"]:
            continue
        if child[0] not in l2g: # the last layer
            continue

        child_gate = l2g[child[0]]
        if child_gate not in parents:
            parents[child_gate] = []
        for p in parents_:
            if type(p[0]) == frozenset:
                for pname in extract_parents(p):
                    if gmodel.get_layer(pname).__class__.__name__ not in ["Conv2D", "Dense", "MultiHeadAttention", "PatchingAndEmbedding"]:
                        continue
                    parent_gate = l2g[pname]
                    parents[child_gate].append(parent_gate)
            else:
                pname = p[0]
                if gmodel.get_layer(pname).__class__.__name__ not in ["Conv2D", "Dense", "MultiHeadAttention", "PatchingAndEmbedding"]:
                    continue
                parent_gate = l2g[pname]
                parents[child_gate].append(parent_gate)

    for key in norm:
        norm[key] = compute_act(gmodel.get_layer(g2l[key]), batch_size)

        if key not in inv_groups:
            continue

        if inv_groups[key] not in contributors:
            contributors[inv_groups[key]] = set()
        contributors[inv_groups[key]].add(g2l[key])

    for child, _ in affecting.items():
        if gmodel.get_layer(child[0]).__class__.__name__ == "DepthwiseConv2D":
            gate = gate_mapping[child][0]["config"]["name"]
            norm[gate] += compute_act(gmodel.get_layer(child[0]), batch_size)
            contributors[inv_groups[gate]].add(child[0])

    gnorm = [0 for _ in range(len(groups))]
    visit = set()
    for c, p in parents.items():
        if len(p) == 0: # first gate
            continue

        for p_ in p:
            gidx = inv_groups[p_]
            cgidx = inv_groups[c]
            if (gidx, cgidx) in visit:
                continue
            visit.add((gidx, cgidx))

            if gidx == cgidx:
                continue

            for l in groups[cgidx]:
                if type(groups[cgidx]) == dict:
                    if type(l) == str:
                        gnorm[gidx] += compute_act(gmodel.get_layer(l), batch_size, pruning_input_gate=True, out_gate=out_gate)
                else:
                    out_gate = gmodel.get_layer(l.name).gates.numpy()
                    gnorm[gidx] += compute_act(gmodel.get_layer(g2l[l.name]), batch_size, pruning_input_gate=True, out_gate=out_gate)

    final_norm = [0 for _ in range(len(groups))]
    for gidx in range(len(groups)):
        for l in groups[gidx]:
            if type(groups[cgidx]) == dict:
                if type(l) == str:
                    final_norm[gidx] += norm[l2g[l]]
            else:
                final_norm[gidx] += norm[l.name]
        final_norm[gidx] += gnorm[gidx]

    for gidx in range(len(final_norm)):
        final_norm[gidx] = float(max(final_norm[gidx], 1.0)) / 1e6

    return final_norm, parents, g2l, contributors


class PruningCallback(keras.callbacks.Callback):

    def __init__(self,
                 norm,
                 targets,
                 gate_groups=None,
                 inv_groups = None,
                 target_ratio=0.5,
                 period=10,
                 l2g = None,
                 num_remove=1,
                 enable_distortion_detect=False,
                 fully_random=False,
                 callback_after_deletion=None,
                 compute_norm_func=None,
                 batch_size=32,
                 gmodel=None,
                 logging_=False):
        super(PruningCallback, self).__init__()
        self.norm = norm
        self.targets = targets
        self.period = period
        self.target_ratio = target_ratio
        self.continue_pruning = True
        self.gate_groups = gate_groups
        self.inv_groups = inv_groups

        self.cnt_after_fail = 0
        self.alpha = 0.1

        self._iter = 0
        self._num_removed = 0
        self.l2g = l2g
        self.num_remove = num_remove
        self.enable_distortion_detect = enable_distortion_detect
        self.fully_random = fully_random
        self.callback_after_deletion = callback_after_deletion
        self.compute_norm_func = compute_norm_func
        self.gmodel = gmodel
        self.batch_size = batch_size
        self.logging = logging_
        if self.logging:
            self.logs = []
        else:
            self.logs = None

        self.subnets = []

    def build_subnets(self, positions, custom_objects=None):

        self.subnets = []
        self.inv_l2s = {}
        for idx in range(len(positions)):

            if idx == 0:
                _g = [None, positions[idx]]
            elif idx == len(positions)-1:
                _g = [positions[idx], None]
            else:
                _g = positions[idx-1:idx+1]

            if custom_objects is None:
                custom_objects = {"SimplePruningGate":SimplePruningGate, "StopGradientLayer":StopGradientLayer}
            parser_ = NNParser(self.gmodel, custom_objects=custom_objects)
            parser_.parse()

            if _g[0] is None:
                _g.remove(None)
                if type(self.gmodel.input) == list:
                    for in_ in self.gmodel.input:
                        _g.append(in_.name)
                else:
                    _g.append(self.gmodel.input.name)
            elif _g[-1] is None:
                _g.remove(None)

                if type(self.gmodel.output) == list:
                    for out in self.gmodel.output:
                        for layer in self.gmodel.layers:
                            if layer.output == out:
                                _g.append(layer.name)
                                break
                else:
                    for layer in self.gmodel.layers:
                        if type(layer.output) == type(self.gmodel.output)\
                            and layer.output.name == self.gmodel.output.name:
                            _g.append(layer.name)
                            break
              
            v = parser_.traverse()
            torder_ = {
                name:idx_
                for idx_, (name, _) in enumerate(v)
            }
            min_t = -1
            max_t = -1
            for l_ in _g:
                if torder_[l_] > max_t:
                    max_t = torder_[l_]
                if min_t == -1 or min_t > torder_[l_]:
                    min_t = torder_[l_]

            def stop_cond(e, inbound, is_edge):
                if not is_edge:
                    return False
                src, dst, level_change, inbound_idx = e[0], e[1], e[2]["level_change"], e[2]["inbound_idx"]

                if not inbound and torder_[dst] > max_t:
                        return True
                if inbound and torder_[src] < min_t:
                    return True

            outbound_cond = lambda e, is_edge: stop_cond(e, False, is_edge)
            inbound_cond = lambda e, is_edge: stop_cond(e, True, is_edge)
            sources = [ x for x in parser_._graph.nodes(data=True) if x[1]["layer_dict"]["config"]["name"] in _g ]
            visit_ = set()
            for s in sources:
                if s[1]["nlevel"] == 0:
                    visit_.add((s[0], 0))
                else:
                    for level in range(s[1]["nlevel"]):
                        visit_.add((s[0], level))
            visit__ = copy.deepcopy(visit_)
            v = parser_.traverse(sources=sources, stopping_condition=outbound_cond, previsit=visit__, sync=False)

            visit__ = copy.deepcopy(visit_)
            v2 = parser_.traverse(sources=sources, stopping_condition=inbound_cond, previsit=visit__, sync=False, inbound=True)

            v = set(v)
            v2 = set(v2)
            v = v.intersection(v2)
            __g = []
            for i in v:
                __g.append(i[0])

            __g_instance = [
                self.gmodel.get_layer(l) for l in __g
            ]

            for l in __g_instance:
                if l.name not in self.inv_l2s:
                    self.inv_l2s[l.name] = []
                if idx not in self.inv_l2s[l.name]:
                    self.inv_l2s[l.name].append(idx)

            subnet, inputs, outputs = parser_.get_subnet(__g_instance, self.gmodel, custom_objects=custom_objects)
            #tf.keras.utils.plot_model(subnet, "subnet"+str(idx)+".png")
            self.subnets.append((subnet, inputs, outputs))

        self.data_holder = [
            []
            for _ in range(len(self.subnets))
        ]


    def on_train_batch_end(self, batch, logs=None, pbar=None, model_=None):
        self._iter += 1
        if self._iter % (self.period // hvd.size()) == 0 and self.continue_pruning:

            if self.compute_norm_func is not None:
                self.norm, parents, g2l, contributors = self.compute_norm_func()

            if self.gate_groups is not None:
                groups = self.gate_groups
            else:
                groups = [
                    [gate] for gate in self.targets
                ]

            cscore_ = {}
            for gidx, group in enumerate(groups):

                if type(group) == dict:
                    for l in group:
                        if type(l) == str:
                            num_batches = len(self.gmodel.get_layer(self.l2g[l]).grad_holder)
                            break

                    items = []
                    max_ = 0
                    for key, val in group.items():
                        if type(key) == str:
                            val = sorted(val, key=lambda x:x[0])
                            items.append((key, val))
                            for v in val:
                                if v[1] > max_:
                                    max_ = v[1]
                    sum_ = np.zeros((max_,))
                    cscore = None
                    for bidx in range(num_batches):
                        for key, val in items:
                            gate = self.gmodel.get_layer(self.l2g[key])
                            grad = gate.grad_holder[bidx]
                            if type(grad) == int and grad == 0:
                                continue

                            grad = pow(grad, 2)
                            grad = tf.reduce_sum(grad, axis=0)
                            for v in val:
                                sum_[v[0]:v[1]] += grad

                        cscore = 0.0
                else:
                    # compute grad based si
                    num_batches = len(group[0].grad_holder)

                    sum_ = 0
                    cscore = None
                    for bidx in range(num_batches):
                        grad = 0
                        for lidx, layer in enumerate(group):
                            grad += layer.grad_holder[bidx]

                        if type(grad) == int and grad == 0:
                            continue

                        grad = pow(grad, 2)
                        sum_ += tf.reduce_sum(grad, axis=0)
                        cscore = 0.0

                # compute normalization
                if cscore is not None: # To handle cscore is undefined.
                    #cscore = sum_
                    cscore = sum_ / self.norm[gidx]
                cscore_[gidx] = cscore

                if self.logs is not None and len(self.logs) == 0:
                    self.logs.append((group, cscore))
                    print([g.name for g in group])
                    for ii in range(cscore.shape[0]):
                        print(ii, float(cscore[ii]))
                    import sys
                    sys.exit(0)

            num_removed_channels = 0
            filtered = set()
            if self.enable_distortion_detect:
                indices = set()

            to_update = {}
            for __ in range(self.num_remove):
                min_val = -1
                min_idx = (-1, -1)
                for gidx, group in enumerate(groups):

                    cscore = cscore_[gidx]
                    if type(group) == dict:
                        items = []
                        max_ = 0
                        for key, val in group.items():
                            if type(key) == str:
                                val = sorted(val, key=lambda x:x[0])
                                items.append((key, val))
                                for v in val:
                                    if v[1] > max_:
                                        max_ = v[1]
                        mask = np.zeros((max_,))
                        for key, val in items:
                            gate = self.gmodel.get_layer(self.l2g[key])
                            for v in val:
                                mask[v[0]:v[1]] += gate.gates.numpy()
                        gates_ = (mask >= 1.0).astype(np.float32)

                    else:
                        gates_ = group[0].gates.numpy()

                    if np.sum(gates_) < 2.0:
                        continue
                    if cscore is not None:
                        if self.fully_random:
                            cscore = np.random.rand(*tuple(cscore.shape))
                        else:
                            min_val, min_idx = find_min(cscore, gates_, min_val, min_idx, gidx, cscore.shape[0])

                filtered.add(min_idx)
                min_group = groups[min_idx[0]]
                if type(min_group) != dict:
                    for min_layer in min_group:
                        gates_ = min_layer.gates.numpy()
                        gates_[min_idx[1]] = 0.0
                        min_layer.gates.assign(gates_)
                        if min_layer.gates.name not in to_update:
                            to_update[min_layer.gates.name] = min_layer.gates
                else:
                    for key, val in min_group.items():
                        if type(key) == str:
                            val = sorted(val, key=lambda x:x[0])
                            gate = self.gmodel.get_layer(self.l2g[key])
                            for v in val:
                                if v[0] <= min_idx[1] and min_idx[1] < v[1]:
                                    gates_ = gate.gates.numpy()
                                    gates_[min_idx[1] - v[0]] = 0.0
                                    gate.gates.assign(gates_)
                                    if gate.gates.name not in to_update:
                                        to_update[gate.gates.name] = gate.gates

                num_removed_channels += 1
                self._num_removed += 1

                exit = False
                if self.enable_distortion_detect:
                    for min_layer in min_group:
                        if min_layer.name not in self.inv_l2s:
                            continue

                        subnet_idx = self.inv_l2s[min_layer.name]
                        if type(subnet_idx) == list:
                            for idx in subnet_idx:
                                indices.add(idx)
                        else:
                            indices.add(subnet_idx)

                    if __ % 10 == 0:
                        total_diff = 0
                        for subnet_idx in indices:
                            sum_diff = 0
                            data_holder = self.data_holder[subnet_idx]
                            mean_val = 0
                            for bidx  in range(len(data_holder)):
                                data = data_holder[bidx]
                                ins, outs = data
                                subnet, _, _ = self.subnets[subnet_idx]
                                if type(subnet.input) != list:
                                    output = subnet(ins[0])
                                else:
                                    output = subnet(ins)

                                if type(subnet.output) != list:
                                    #left = tf.reshape(output, (output.shape[0], -1))
                                    #right = tf.reshape(outs[0], (outs[0].shape[0], -1))
                                    left = output
                                    right = outs[0]
                                    mean_val += np.mean(abs(right))
                                    sum_diff += tf.math.reduce_mean(tf.keras.losses.mean_squared_error(left, right))
                                    #sum_diff += tf.abs(tf.norm(left) - tf.norm(right)) / tf.norm(left)
                                else:
                                    sum_diff_ = 0
                                    mean_val_ = 0
                                    for left, right in zip(output, outs):
                                        sum_diff_ += tf.math.reduce_mean(tf.keras.losses.mean_squared_error(left, right))
                                        #sum_diff += tf.abs(tf.norm(left) - tf.norm(right)) / tf.norm(left)
                                        mean_val_ += np.mean(abs(right))
                                    sum_diff += sum_diff_ / len(output)
                                    mean_val += mean_val_ / len(output)

                            sum_diff /= len(data_holder)
                            mean_val /= len(data_holder)
                            #print(pow(0.1 * mean_val, 2), sum_diff)
                            if sum_diff > pow(self.alpha * mean_val, 2):
                                exit = True
                                break

                        if not exit:
                            indices.clear()
                            filtered.clear()

                if exit: # restore the last removed channel                    
                    for min_idx_ in filtered:
                        min_group = groups[min_idx_[0]]

                        if type(min_group) != dict:
                            for min_layer in min_group:
                                gates_ = min_layer.gates.numpy()
                                gates_[min_idx_[1]] = 1.0
                                min_layer.gates.assign(gates_)
                                if min_layer.gates.name not in to_update:
                                    to_update[min_layer.gates.name] = min_layer.gates
                        else:
                            for key, val in min_group.items():
                                if type(key) == str:
                                    val = sorted(val, key=lambda x:x[0])
                                    gate = self.gmodel.get_layer(self.l2g[key])
                                    for v in val:
                                        if v[0] <= min_idx[1] and min_idx[1] < v[1]:
                                            gates_ = gate.gates.numpy()
                                            gates_[min_idx[1] - v[0]] = 1.0
                                            gate.gates.assign(gates_)
                                            if gate.gates.name not in to_update:
                                                to_update[gate.gates.name] = gate.gates

                        self._num_removed -= 1
                        num_removed_channels -= 1

                    if num_removed_channels <= 1:
                        self.cnt_after_fail += 1
                        if self.cnt_after_fail == 1:
                            self.cnt_after_fail = 0
                            self.alpha += 0.05
                    else:
                        self.cnt_after_fail = 0

                    break

                # score update
                if self.compute_norm_func is not None:
                    cscore_[min_idx[0]] *= self.norm[min_idx[0]]
                    visit = set()
                    base_norm_sum = 0
                    delta1 = 0
                    delta2 = 0
                    #for min_layer in min_group:
                    #    w = self.gmodel.get_layer(g2l[min_layer.name]).get_weights()[0].shape
                    #    delta += float(max(self.batch_size * np.prod(list(w[0:2])), 1.0)) / 1e6
                    for cbt in contributors[min_idx[0]]:
                        if self.gmodel.get_layer(cbt).__class__.__name__ not in ["Conv2D", "DepthwiseConv2D", "Dense"]:
                            continue
                        w = self.gmodel.get_layer(cbt).get_weights()[0].shape
                        if self.gmodel.get_layer(cbt).__class__.__name__ == "Conv2D":
                            delta1 += float(max(self.batch_size * np.prod(list(w[0:2])), 1.0)) / 1e6
                        else:
                            delta2 += float(max(self.batch_size * np.prod(list(w[0:2])), 1.0)) / 1e6
                    self.norm[min_idx[0]] -= (delta1+delta2)
                    cscore_[min_idx[0]] /= self.norm[min_idx[0]]

                    for min_layer in min_group:
                        for p in parents[min_layer.name]:
                            if cscore_[self.inv_groups[p]] is None:
                                continue

                            if (min_idx[0], self.inv_groups[p]) in visit or min_idx[0] == self.inv_groups[p]:
                                continue
                            visit.add((min_idx[0], self.inv_groups[p]))
                            cscore_[self.inv_groups[p]] *= self.norm[self.inv_groups[p]]
                            self.norm[self.inv_groups[p]] -= delta1
                            cscore_[self.inv_groups[p]] /= self.norm[self.inv_groups[p]]

                if self.callback_after_deletion is not None:
                   self.callback_after_deletion(self._num_removed)

                if compute_sparsity(groups, self.l2g, self.gmodel) >= self.target_ratio:
                    break

            if hvd.size() > 1:
                to_update_  = [to_update[key] for key in to_update]
                hvd.broadcast_variables(model_.variables, root_rank=0)

            self.continue_pruning = compute_sparsity(groups, self.l2g, self.gmodel) < self.target_ratio
            for layer in self.targets:
                layer.grad_holder = []
                if not self.continue_pruning:
                    layer.collecting = False

            self.data_holder = [
                []
                for _ in range(len(self.subnets))
            ]

            if pbar is not None:
                pbar.set_postfix({"Sparsity":compute_sparsity(groups, self.l2g, self.gmodel), "Num removed(last step)":num_removed_channels})

            # for fit
            if not self.continue_pruning and hasattr(self, "model") and hasattr(self.model, "stop_training"):
                self.model.stop_training = True

            return num_removed_channels
        else:
            return 0

def is_simple_group(g):
    for l in g:
        if type(l) != str:
            return False
    return True

def make_group_fisher(model,
                      model_handler,
                      batch_size,
                      custom_objects=None,
                      avoid=None,
                      period=25,
                      target_ratio=0.5,
                      enable_norm=True,
                      norm_update=False,
                      num_remove=1,
                      enable_distortion_detect=False,
                      fully_random=False,
                      save_steps=-1,
                      save_prefix=None,
                      save_dir=None,
                      logging_=False):

    gmodel, model, l2g, ordered_groups, torder, parser, gate_mapping = add_gates(model, custom_objects, avoid)
    targets = find_all(gmodel, SimplePruningGate)

    tf.keras.utils.plot_model(gmodel, "gmodel.pdf", show_shapes=True)

    last = parser.get_last_transformers()

    groups = []
    for g, _ in ordered_groups:
        if is_simple_group(g):
            gate_group = []
            is_last = False
            for l in g:
                if l in last:
                    is_last = True
                    break
            if is_last:
                continue
            for l in g:
                gate_group.append(gmodel.get_layer(l2g[l]))
            groups.append(gate_group)
        else:
            g_ = flatten(g)
            is_last = False
            for l in g_:
                if l in last:
                    is_last = True
                    break
            if is_last:
                continue
            _, group_struct = parser.get_group_topology(g_)
            groups.append(group_struct[0]) # g_ must be a single group

    inv_groups = {}
    for idx, g in enumerate(groups):
        if type(g) == list:
            for l in g:
                inv_groups[l.name] = idx 
        else:
            for key in g:
                if type(key) == str:
                    inv_groups[l2g[key]] = idx
            
    # Compute normalization score
    if enable_norm:
        norm, parents, g2l, contributors = compute_norm(parser, gate_mapping, gmodel, batch_size, targets, groups, inv_groups, l2g)
    else:
        norm = {
            t.name: 1.0
            for t in targets
        }

    def callback_after_deletion_(num_removed):
        if num_removed % save_steps == 0 and hvd.rank() == 0:
            assert save_dir is not None
            assert save_prefix is not None
            cmodel = parser.cut(gmodel)
            tf.keras.models.save_model(cmodel, save_dir+"/"+save_prefix+"_"+str(num_removed)+".h5")
            tf.keras.models.save_model(gmodel, save_dir+"/"+save_prefix+"_"+str(num_removed)+"_gated_model.h5")
            del cmodel

    if save_steps == -1:
        cbk = None
    else:
        cbk = callback_after_deletion_

    if norm_update:
        norm_func = lambda : compute_norm(parser, gate_mapping, gmodel, batch_size, targets, groups, inv_groups, l2g)
    else:
        norm_func = None
    return gmodel, model, parser, ordered_groups, torder, PruningCallback(
        norm,
        targets,
        gate_groups=groups,
        inv_groups=inv_groups,
        period=period,
        target_ratio=target_ratio,
        l2g=l2g,
        num_remove=num_remove,
        enable_distortion_detect=enable_distortion_detect,
        compute_norm_func=norm_func,
        fully_random=fully_random,
        callback_after_deletion=cbk,
        batch_size=batch_size,
        gmodel=gmodel,
        logging_=logging_)


def prune_step(X, model, teacher_logits, y, pc, print_by_pruning, pbar=None):

    if pc.continue_pruning:
        for layer in model.layers:
            if layer.__class__ == SimplePruningGate:
                layer.trainable = True
                layer.collecting = True

        tape, loss, position_output = train_step(X, model, teacher_logits, y, ret_last_tensor=True)
        tape = hvd.DistributedGradientTape(tape)
        _ = tape.gradient(loss, model.trainable_variables)

        ret = pc.on_train_batch_end(None, pbar=pbar, model_=model)

        if pc.enable_distortion_detect:
            for idx in range(len(pc.subnets)):
                pc.data_holder[idx].append(position_output[idx])

        for layer in model.layers:
            if layer.__class__ == SimplePruningGate:
                layer.trainable = False
                layer.collecting = False
    else:
        ret = 0        

    if print_by_pruning:
        return ret
    else:
        return None
