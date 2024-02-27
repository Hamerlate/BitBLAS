# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import tvm
from tvm.target import Target
from typing import Literal
from .operator import Operator
from .impl.lop3_permutate_impl import select_implementation
from dataclasses import dataclass
from bitblas.utils.tensor_adapter import tvm_tensor_to_torch
import torch

@dataclass
class LOP3PermutateConfig:
    M: int
    N: int
    datatype: Literal["float16", "int8"] = "float16"
    storage_dtype: Literal["int8", "uint8", "int32", "uint32"] = "int32"
    dequantize_bits: int = 4


class LOP3Permutate(Operator):
    def __init__(
        self,
        config: LOP3PermutateConfig,
        name: str = "permutate",
        target: Target = tvm.target.Target("llvm"),  # assume to do permutation on gpu.
    ):
        # consider to warp the arguments to MatmulConfig
        super().__init__(name, target)
        self.config = config

        if target.kind.name != "llvm":
            raise ValueError("Currently only support llvm target for Permutation")

        prim_func_mod = self._select_implementation()
        self.prim_func_mod = prim_func_mod
        self.target = target
        self._build_runtime_module(target)

    def _select_implementation(self):
        return select_implementation(
            M=self.M,
            N=self.N,
            datatype=self.datatype,
            dequantize_bits=self.dequantize_bits,
        )

    def forward_from_torch(self, weight, res):
        # reintepret the input tensor to int32 format
        _tvm_args = [self._tensor_adapter(arg.view(torch.int32), self.arch.device) for arg in [weight, res]]
        self.rt_mod(*_tvm_args)
        return tvm_tensor_to_torch(_tvm_args[-1]).view(weight.dtype)

    @property
    def M(self):
        return self.config.M

    @property
    def N(self):
        return self.config.N

    @property
    def datatype(self):
        return self.config.datatype

    @property
    def storage_dtype(self):
        return self.config.storage_dtype

    @property
    def dequantize_bits(self):
        return self.config.dequantize_bits


__all__ = ["LOP3Permutate", "LOP3PermutateConfig"]