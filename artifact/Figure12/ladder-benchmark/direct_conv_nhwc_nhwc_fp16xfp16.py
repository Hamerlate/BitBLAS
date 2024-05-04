import numpy as np
import tvm
import ladder
from tvm import relay
import os.path as osp
from tvm.contrib.target.onnx import to_onnx
from tvm.relay.testing import run_infer_type
from tvm.contrib import graph_executor
from ladder.te_utils import connect_tensor_graph
from tvm import te, tir
from ladder.graph import IRNode, OutputNode
from ladder.policy import *
from ladder.reference import get_subgraph_reference_outputs
import os
import torch
from tvm.script import tir as T
# get file name and remove the suffix
fname = os.path.basename(__file__)
fname = os.path.splitext(fname)[0]
# create log path
log_path = "progress/" + fname

arch = 'cuda'
arch = ladder.arch.__getattribute__(arch)()
dtype="float16"
bit = 8
n_float_per_i8 = 8 // bit
mask = (1 << bit) - 1
def ladder_conv_nhwc_hwnc(n, f, h, w, c, kh, kw, s, d, p):
    
    A = te.placeholder((n, h, w, c), name='input', dtype='float16')
    B = te.placeholder((f, kh, kw, c), name='weight', dtype='float16')
  
    pad_shape = (n, h + 2 * p, w + 2 * p, c)
    pad_value = tir.const(0.0, A.dtype)
    pad = te.compute(
                    pad_shape,
                    lambda n, h, w, c: te.if_then_else(
                        tir.all(
                            h >= p,
                            w >= p,
                            h < pad_shape[1] - p,
                            w < pad_shape[2] - p,
                        ),
                        A[n, h - p, w - p, c],
                        pad_value,
                    ),
                    name="pad",
                )
    kernel_h, kernel_w = kh, kw
    stride_h, stride_w = s, s
    dilation_h, dilation_w = d, d
    out_h = (h + 2 * p - (dilation_h * (kernel_h - 1) + 1)) // stride_h + 1
    out_w = (w + 2 * p - (dilation_w * (kernel_w - 1) + 1)) // stride_w + 1
    out_shape = (n, out_h, out_w, f)
    kh = te.reduce_axis((0, kernel_h), name="kh")
    kw = te.reduce_axis((0, kernel_w), name="kw")
    c = te.reduce_axis((0, c), name="c")
    C = te.compute(
        out_shape,
        lambda n, h, w, f: te.sum(
            pad[
                n,
                h * stride_h + tir.any(dilation_h),
                w * stride_w + tir.any(dilation_w),
                c,
            ]
            * B[f, kh - 1 - tir.any(dilation_h), kw - 1 - tir.any(dilation_w), c],
            axis=[kh, kw, c],
        ),
        name="C",
    )
    return A, B, C

def reshape(_N, _H, _W, _C, wmma_m, wmma_n):
    M = _N * _H * _W
    N = _C
    C = te.placeholder((M // wmma_m, N // wmma_n, wmma_m, wmma_n), name='C', dtype='float16')
    C_reshpae = te.compute(
        (_N // wmma_m, _H, _W, _C // wmma_n, wmma_m, wmma_n),
        lambda n, h, w, c, nn, cc: C[w + _W *h + _W * _H * n, c, nn, cc],
        name='C_reshape'
    )
    return C, C_reshpae

def reshape_nhwc(_N, _H, _W, _C, wmma_m, wmma_n):
    C = te.placeholder((_N // wmma_m, _H, _W, _C // wmma_n, wmma_m, wmma_n), name='C', dtype='float16')
    C_reshpae = te.compute(
        (_N, _H, _W, _C),
        lambda n, h, w, c: C[n // wmma_m, h, w, c // wmma_n, n % wmma_m, c % wmma_n],
        name='C_reshape'
    )
    return C, C_reshpae

def bias(_N, _H, _W, _C):
    A = te.placeholder((_N, _H, _W, _C), name='A', dtype='float16')
    B = te.placeholder((_C,), name='B', dtype='float16')
    C = te.compute(
        (_N, _H, _W, _C),
        lambda n, h, w, c: A[n, h, w, c] + B[c],
        name='C'
    )
    return A, B, C

def relu(_N, _H, _W, _C):
    A = te.placeholder((_N, _H, _W, _C), name='A', dtype='float16')
    B = te.compute(
        (_N, _H, _W, _C),
        lambda n, h, w, c: te.max(A[n, h, w, c], tir.const(0.0, A.dtype)),
        name='B'
    )
    return A, B

def layout_transform(_N, _H, _W, _C, wmma_m = 16, wmma_n = 16):
    A = te.placeholder((_N, _H, _W, _C), name='A', dtype='float16')
    B = te.compute(
        (_N // wmma_m, _H, _W, _C // wmma_n, wmma_m, wmma_n),
        lambda n, h, w, c, nn, cc: A[n * wmma_m + nn, h, w, c * wmma_n + cc],
        name='B'
    )
    return A, B

def add_conv(_N, _H, _W, _C):
    A = te.placeholder((_N, _H, _W, _C), name='A', dtype='float16')
    B = te.placeholder((_N, _H, _W, _C), name='B', dtype='float16')
    C = te.compute(
        (_N, _H, _W, _C),
        lambda n, h, w, c: A[n, h, w, c] + B[n, h, w, c],
        name='C'
    )
    return A, B, C

def layout_transform_nhwc2nchw(_N, _H, _W, _C):
    A = te.placeholder((_N, _H, _W, _C), name='A', dtype='float16')
    B = te.compute(
        (_N, _C, _H, _W),
        lambda n, c, h, w: A[n, h, w, c],
        name='B'
    )
    return A, B

# the topi layout_transform compute does not simplify well when lowering, so we implement a new one here
def A_global_16x16_to_shared_load_16x16_layout(i, j):
    thread_id = i * 2 + j // 8
    row = thread_id % 16
    col = (j % 8) + (thread_id // 16) * 8
    return row, col

def B_global_16x16_to_shared_load_16x16_layout(i, j):
    thread_id = i * 2 + j // 8
    row = (i // 8) * 8 + (thread_id % 8)
    col = (j % 8) + 8 * ((thread_id // 8) % 2)
    return row, col


def layout_transform_with_func(_N, _H, _W, _C, wmma_m = 16, wmma_n = 16, func=None):
    def fcompute(*args):
        warp_i, warp_j = args[-2:]
        spatial_args = args[:-2]
        permutate_i, permutate_j = func(warp_i, warp_j)
        new_index = (*spatial_args, permutate_i, permutate_j)
        return A[new_index]
    A = te.placeholder((_N // wmma_m, _H, _W, _C // wmma_n, wmma_m, wmma_n), name='A', dtype='float16')
    B = te.compute(
        (_N // wmma_m, _H, _W, _C // wmma_n, wmma_m, wmma_n),
        fcompute,
        name='B'
    )
    return A, B

resnet50_shapes = [
    [128, 512, 7, 7, 2048, 1, 1, 1, 1, 0],
    [128, 512, 14, 14, 512, 3, 3, 2, 1, 1],
    [128, 1024, 14, 14, 512, 1, 1, 1, 1, 0],
    [128, 256, 14, 14, 1024, 1, 1, 1, 1, 0],
    [128, 256, 28, 28, 256, 3, 3, 2, 1, 1],
    [128, 512, 28, 28, 256, 1, 1, 1, 1, 0],
    [128, 128, 28, 28, 512, 1, 1, 1, 1, 0],

    [128, 256, 56, 56, 128, 1, 1, 1, 1, 0],
    [128, 64, 56, 56, 256, 1, 1, 1, 1, 0],
    [128, 64, 56, 56, 64, 3, 3, 1, 1, 1],
    [128, 64, 56, 56, 64, 1, 1, 1, 1, 0],
    [128, 256, 56, 56, 64, 1, 1, 1, 1, 0],
    [128, 256, 56, 56, 512, 1, 1, 2, 1, 0],
    [128, 128, 28, 28, 128, 3, 3, 1, 1, 1],
    [128, 512, 28, 28, 128, 1, 1, 1, 1, 0],
    [128, 512, 28, 28, 1024, 1, 1, 2, 1, 0],
    [128, 256, 14, 14, 256, 3, 3, 1, 1, 1],
    [128, 1024, 14, 14, 256, 1, 1, 1, 1, 0],
    [128, 1024, 14, 14, 2048, 1, 1, 2, 1, 0],
    [128, 512, 7, 7, 512, 3, 3, 1, 1, 1],
    [128, 2048, 7, 7, 512, 1, 1, 1, 1, 0],
]
shufflenet_shapes = [
    [128, 464, 7, 7, 1024, 1, 1, 1, 1, 0],
]
unet_shapes = [
    [16, 320, 64, 64, 320, 1, 1, 1, 1, 0],
    [16, 640, 64, 64, 320, 1, 1, 1, 1, 0],
    [16, 960, 64, 64, 320, 1, 1, 1, 1, 0],
    [16, 640, 64, 64, 640, 3, 3, 1, 1, 1],
    [16, 640, 32, 32, 640, 1, 1, 1, 1, 0],
    [16, 960, 32, 32, 640, 1, 1, 1, 1, 0],
    [16, 1280, 32, 32, 640, 1, 1, 1, 1, 0],
    [16, 1920, 32, 32, 640, 1, 1, 1, 1, 0],
    [16, 1280, 32, 32, 1280, 3, 3, 1, 1, 1],
    [16, 1280, 16, 16, 1280, 1, 1, 1, 1, 0],
    [16, 1920, 16, 16, 1280, 1, 1, 1, 1, 0],
    [16, 2560, 16, 16, 1280, 1, 1, 1, 1, 0],
    [16, 1280, 16, 16, 1280, 3, 3, 1, 1, 1],
    [16, 2560, 8, 8, 1280, 1, 1, 1, 1, 0],
    [16, 1280, 8, 8, 1280, 1, 1, 1, 1, 0],
    [16, 1280, 16, 16, 1280, 3, 3, 2, 1, 1],
    [16, 640, 16, 16, 1280, 1, 1, 1, 1, 0],
    [16, 640, 32, 32, 640, 3, 3, 2, 1, 1],
    [16, 320, 32, 32, 640, 1, 1, 1, 1, 0],
    [16, 320, 64, 64, 320, 3, 3, 2, 1, 1],
    [16, 320, 64, 64, 320, 3, 3, 1, 1, 1],
    [16, 640, 32, 32, 640, 3, 3, 1, 1, 1],
    [16, 320, 32, 32, 640, 3, 3, 1, 1, 1],
    [16, 640, 16, 16, 1280, 3, 3, 1, 1, 1],
    [16, 1280, 8, 8, 1280, 3, 3, 1, 1, 1],
    [16, 2560, 8, 8, 1280, 3, 3, 1, 1, 1],
    [16, 2560, 16, 16, 1280, 3, 3, 1, 1, 1],
    [16, 1920, 16, 16, 1280, 3, 3, 1, 1, 1],
    [16, 1920, 32, 32, 640, 3, 3, 1, 1, 1],
    [16, 1280, 32, 32, 640, 3, 3, 1, 1, 1],
    [16, 960, 32, 32, 640, 3, 3, 1, 1, 1],
    [16, 960, 64, 64, 320, 3, 3, 1, 1, 1],
    [16, 640, 64, 64, 320, 3, 3, 1, 1, 1],
]
shapes = resnet50_shapes + shufflenet_shapes + unet_shapes
perf_map = []
for n, c, h, w, f, kh, kw, s, d, p in shufflenet_shapes:
    key = f'{n}_{f}_{h}_{w}_{c}_{kh}_{kw}_{s}_{d}_{p}'
    oh = (h + 2 * p - kh) // s + 1
    ow = (w + 2 * p - kw) // s + 1
    print("n: {}, f: {}, h: {}, w: {}, c: {}, kh: {}, kw: {}, s: {}, d: {}, p: {}, oh: {}, ow: {}".format(n, f, h, w, c, kh, kw, s, d, p, oh, ow))
    compute_flops = 2 * n * f * oh * ow * c * kh * kw
    arg1 = ladder_conv_nhwc_hwnc(n, f, h, w, c, kh, kw, s, d, p)
    arg2 = reshape(n, oh, ow, f, 16, 16)
    arg3 = reshape_nhwc(n, oh, ow, f, 16, 16)
    arg4 = bias(n, oh, ow, f)
    arg5 = add_conv(n, oh, ow, f)
    arg6 = relu(n, oh, ow, f)
    arg7 = layout_transform(n, oh, ow, f)
    arg8 = layout_transform_with_func(n, oh, ow, f, func=A_global_16x16_to_shared_load_16x16_layout)
    arg9 = layout_transform_nhwc2nchw(n, oh, ow, f)
    
    args = arg1
    # args = tuple(connect_tensor_graph(args, arg2, {arg2[0]:args[-1]}))
    # args = tuple(connect_tensor_graph(args, arg3, {arg3[0]:args[-1]}))
    # args = tuple(connect_tensor_graph(args, arg4, {arg4[0]:args[-1]}))
    # args = tuple(connect_tensor_graph(args, arg5, {arg5[0]:args[-1]}))
    # args = tuple(connect_tensor_graph(args, arg6, {arg6[0]:args[-1]}))
    # args = tuple(connect_tensor_graph(args, arg7, {arg7[0]:args[-1]}))
    # args = tuple(connect_tensor_graph(args, arg8, {arg8[0]:args[-1]}))

    input_args = args[:-1]
    output_args = [args[-1]]
    node = IRNode([None for _ in input_args], args, "ladder_conv2d_reshape_bias")
    # node.add_tag("consistent_config", (True, True))
    output_nodes = [OutputNode(node)]
    policy = DefaultPolicy(output_nodes, arch)
    configs = policy.emit_config(40)

    compile_results = []
    cgen = ladder.CodeGenerator()
    for config in configs:
        try:
            cpresult = cgen.compile(output_nodes, config, "cuda", kernel_name="Fused")
        except:
            continue
        compile_results.append(cpresult)
    ladder.utils.compile_and_load_parallel(compile_results, arch)
    best_latency = 10000
    best = None
    values = []
    for cpresult in compile_results:
        print(cpresult.config)
        code = cpresult.code
        if cpresult.lib is None:
            latency = 10000 
        else:
            latency = cpresult.profile()
        values.append(latency)
        if latency < best_latency:
            best_latency = latency
            best = cpresult
        print(latency)
    print('code: ', code)
    print("top1: {} \ttop10: {}".format(values[0], min(values)))
    print("-" * 80, flush=True)
    print("best config: {}".format(best.config))
    print("best latency: {}".format(best_latency))
    print(f"{(compute_flops/(best_latency * 1e-3))/ pow((1024), 4)} tflops, {(compute_flops/(best_latency * 1e-3))/ pow((1024), 4) / 145 * 100} %")
    
    perf_map.append((key, best_latency))

for key, latency in perf_map:
    print("{}\t{}".format(key, latency))
