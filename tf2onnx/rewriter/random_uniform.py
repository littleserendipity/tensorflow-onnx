# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.rewriter - rewrite tensorflow subgraph to onnx random_uniform op
"""
import numpy as np
from tf2onnx.graph_matcher import OpTypePattern, GraphMatcher
from tf2onnx import utils, handler


# pylint: disable=missing-docstring


def rewrite_random_uniform(g, ops):
    pattern = \
        OpTypePattern('Add', name='output', inputs=[
            OpTypePattern('Mul', inputs=[
                OpTypePattern('RandomUniform', name='input1', inputs=["*"]),
                OpTypePattern('Sub', name='input2', inputs=["*", "*"]),
            ]), None
        ])

    matcher = GraphMatcher(pattern)
    match_results = list(matcher.match_ops(ops))
    for match in match_results:
        input2 = match.get_op('input2')
        output = match.get_op('output')
        ru_op = match.get_op('input1')
        # max is on input 0
        tmax = input2.inputs[0].get_tensor_value()
        tmin = input2.inputs[1].get_tensor_value()
        to_delete = list(set(match.get_nodes()))
        new_node = create_onnx_random_uniform_op(g, tmax, tmin, ru_op, output, to_delete)
        g.replace_all_inputs(ops, output.output[0], new_node.output[0])
        for n in to_delete:
            g.remove_node(n.name)

    return ops


# rewriter function when fold_const is enabled
def rewrite_random_uniform_fold_const(g, ops):
    pattern = \
        OpTypePattern('Add', name='output', inputs=[
            OpTypePattern('Mul', name='mul', inputs=[
                OpTypePattern('RandomUniform', name='input1', inputs=["*"]),
                None,
            ]),
            None,
        ])

    matcher = GraphMatcher(pattern)
    match_results = list(matcher.match_ops(ops))
    for match in match_results:
        output = match.get_op('output')
        mul = match.get_op('mul')
        ru_op = match.get_op('input1')

        tmax_minus_tmin = mul.inputs[1].get_tensor_value()
        tmin = output.inputs[1].get_tensor_value()
        tmax = tmin + tmax_minus_tmin
        new_node = create_onnx_random_uniform_op(g, tmax, tmin, ru_op, output, to_delete)
        g.replace_all_inputs(ops, output.output[0], new_node.output[0])
        to_delete = list(set(match.get_nodes()))
        for n in to_delete:
            g.remove_node(n.name)

    return ops


def create_onnx_random_uniform_op(g, tmax, tmin, ru_op, output, to_delete):
    dtype = g.get_dtype(output.output[0])
    op_name = utils.make_name("RandomUniform")
    shape_node = ru_op.inputs[0]
    shape = g.get_shape(output.output[0])
    if shape_node.is_const():
        # if the tensorflow input (aka the shape) is const we can use the RandomUniform op
        new_node = g.make_node("RandomUniform", [], name=op_name,
                               attr={"low": tmin, "high": tmax, "dtype": dtype, "shape": shape},
                               shapes=[shape], dtypes=[dtype])
    else:
        if shape_node.type == "Shape":
            # if shape is dynamic - in tensorflow shape comes as tensor VALUE,
            # in onnx RandomUniformLike finds takes the shape from the tensor itself.
            # In many cases there is a shape op in tensorflow before RandomUniform and
            # to make that work for onnx we just need to remove the shape op.
            new_node = g.make_node("RandomUniformLike", inputs=[shape_node.input[0]], name=op_name,
                                   attr={"low": tmin, "high": tmax, "dtype": dtype},
                                   shapes=shape, dtypes=[dtype])
        else:
            # if the shape is calculated we need to create a tensor so RandomUniformLike
            # can take the shape from there. Pre opset9 this is somewhat hacky because there is
            # no real fill op in onnx. In general this is not going to help performance but the tensors
            # created are expected to be small.

            # tell the caller to not delete the shape node
            to_delete.remove(shape_node)
            # create a fill op with the shape of the value of the input tensor
            zero = g.make_const(utils.make_name("zero"), np.zeros((), dtype=np.float32))
            fill_node = g.make_node("Fill", inputs=[shape_node.output[0], zero.name],
                                    shapes=shape, dtypes=[dtype])
            func, _ = handler.tf_op.find_effective_op("Fill")
            func(g, fill_node)
            # and use RandomUniformLike to create the random tensor
            new_node = g.make_node("RandomUniformLike", inputs=[fill_node.output[0]], name=op_name,
                                   attr={"low": tmin, "high": tmax, "dtype": dtype},
                                   shapes=shape, dtypes=[dtype])
    return new_node
