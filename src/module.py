import math
import typing

import revlib
import torch
import torch.nn.functional

QUAD_TENSOR = typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]


@torch.jit.script
def _activate_norm(fn_input: torch.Tensor) -> torch.Tensor:
    out = torch.nn.functional.relu(fn_input)
    out = out - out.mean(-1, keepdim=True)
    return out / ((out.square().sum(-1, keepdim=True).sqrt() + 1e-5) * out.size(-1) ** -0.5)


def output(hidden_features, out_features):
    return torch.nn.Conv1d(hidden_features, out_features, 1)


@torch.jit.script
def conv(inp: torch.Tensor, weight: torch.Tensor, kernel_size: int) -> torch.Tensor:
    return torch.nn.functional.conv1d(torch.nn.functional.pad(inp, (kernel_size - 1, 0)), weight)


@torch.jit.script
def feed_forward(inp: torch.Tensor, weight0: torch.Tensor, weight1: torch.Tensor, kernel_size: int) -> torch.Tensor:
    return conv(_activate_norm(conv(inp, weight0, kernel_size)), weight1, kernel_size)


class FeedForward(torch.nn.Module):
    def __init__(self, hidden_features, kernel_size=7, intermediate_factor=1):
        super().__init__()
        self.kernel_size = kernel_size
        self.w0 = torch.nn.Conv1d(hidden_features, hidden_features * intermediate_factor, kernel_size,
                                  bias=False).weight
        self.w1 = torch.nn.Conv1d(hidden_features * intermediate_factor, hidden_features, kernel_size,
                                  bias=False).weight

    def forward(self, inp: torch.Tensor):
        return feed_forward(inp, self.w0, self.w1, self.kernel_size)


@torch.jit.script
def linear_attention(inp: torch.Tensor, depth: torch.Tensor, point: torch.Tensor, shift: torch.Tensor,
                     divisor: torch.Tensor) -> torch.Tensor:
    return _activate_norm(inp * (depth.cumsum(1) / divisor + point) + shift)


class LinearAttentionCell(torch.nn.Module):
    def __init__(self, hidden_features, base):
        super(LinearAttentionCell, self).__init__()
        self.pos_embd = lambda: base.pos_embd
        self.divisor = lambda: base.divisor
        self.depth = FeedForward(hidden_features)
        self.point = FeedForward(hidden_features)
        self.shift = FeedForward(hidden_features)

    def forward(self, inp: torch.Tensor) -> torch.Tensor:
        out = inp + self.pos_embd()
        return linear_attention(inp, self.depth(out), self.point(out), self.shift(out), self.divisor())


class LinearAttention(torch.nn.Module):
    """
    One idea would be to run linear attention at every step in an rnn
    """

    def __init__(self, input_cases, hidden_features, out_features, delay=8, input_count=0, embedding_std=1):
        super(LinearAttention, self).__init__()
        self.embedding = torch.nn.Parameter(torch.randn((input_cases, hidden_features * 2)).mul(embedding_std))

        pos_embd = torch.arange(0, input_count).unsqueeze(0) + 1
        feature_embd = torch.arange(0, hidden_features).unsqueeze(1) + 1
        additive = (feature_embd % 2).to(torch.float)
        feature_embd = (feature_embd - additive) / 2
        additive *= math.pi
        feature_embd *= 8 / hidden_features
        feature_embd -= math.log(input_count / 2 / math.pi)
        feature_embd = torch.exp(feature_embd) + additive
        self.register_buffer("pos_embd", torch.sin(pos_embd * feature_embd).mul(embedding_std / delay).unsqueeze(0))
        self.register_buffer("divisor", pos_embd.unsqueeze(0).to(torch.float))
        self.stem = revlib.ReversibleSequential(*[LinearAttentionCell(hidden_features, self) for _ in range(delay)])
        self.output = output(hidden_features * 2, out_features)

    def forward(self, inp: torch.Tensor, tgt: torch.Tensor):
        return torch.nn.functional.cross_entropy(self.output(self.stem(self.embedding[inp].transpose(1, 2))), tgt)