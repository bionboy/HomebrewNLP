import torch


def _activate_norm(fn_input: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.relu(fn_input)


def _single_calc(fn_input, sequence_input, linear_param):
    features_sqrt = fn_input.size(2)
    batch = fn_input.size(0)
    features = features_sqrt ** 2
    fn_input = fn_input.view(batch, features)
    fn_input = _activate_norm(fn_input)
    b = torch.mm(fn_input, linear_param[:features])
    c = torch.mm(sequence_input, linear_param[features:features * 2])
    o = _activate_norm(b * c)
    o = torch.mm(o, linear_param[features * 2:])
    o = o.view(batch, features_sqrt, features_sqrt)
    return o.qr().Q


def _calc(fn_input: torch.Tensor, sequence_input: torch.Tensor, linear_param: torch.Tensor, depth: int):
    out = fn_input
    for idx in range(depth):
        out = _single_calc(out, sequence_input, linear_param[idx])
    return out


def _forward_pass(fn_input: torch.Tensor, sequence_input: torch.Tensor, linear_param0: torch.Tensor,
                  linear_param1: torch.Tensor, depth: int):
    inp = fn_input.chunk(2, 1)
    outputs = [None, None]
    outputs[1] = torch.bmm(inp[1], _calc(inp[0], sequence_input, linear_param0, depth))
    outputs[0] = torch.bmm(inp[0], _calc(outputs[1], sequence_input, linear_param1, depth))
    out = torch.cat(outputs, 1)
    return out


def _backward_one(out: torch.Tensor, inp: torch.Tensor, sequence_input: torch.Tensor, linear_param: torch.Tensor,
                  depth: int):
    return torch.bmm(out, _calc(inp, sequence_input, linear_param, depth).transpose(1, 2))


class ReversibleRNNFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, fn_input, sequence_input, linear_param0, linear_param1, output_list, top, depth, embedding):
        ctx.save_for_backward(sequence_input, linear_param0, linear_param1, embedding)
        sequence_input = embedding[sequence_input]
        ctx.output_list = output_list
        ctx.top = top
        ctx.depth = depth

        if output_list:
            output_list.clear()
        with torch.no_grad():
            out = _forward_pass(fn_input, sequence_input, linear_param0, linear_param1, depth)
        with torch.enable_grad():
            out.requires_grad_(True)
            output_list.append(out)
            return out

    @staticmethod
    def backward(ctx, grad_output):
        sequence_input, linear_param0, linear_param1, embedding = ctx.saved_tensors
        sequence_input = embedding[sequence_input]
        depth = ctx.depth
        if not sequence_input.requires_grad:
            return (None,) * 9

        out = ctx.output_list.pop(0)
        features = out.size(1) // 2
        out0, out1 = out[:, :features], out[:, features:]
        with torch.no_grad():
            inp0 = _backward_one(out0, out1, sequence_input, linear_param1, depth)
            inp1 = _backward_one(out1, inp0, sequence_input, linear_param0, depth)
        with torch.enable_grad():
            fn_input = torch.cat([inp0, inp1], 1)
            fn_input.detach_()
            fn_input.requires_grad_(True)
            args = (fn_input, sequence_input, linear_param0, linear_param1, depth)
            grad_out = _forward_pass(*args)
        grad_out.requires_grad_(True)
        grad_out = torch.autograd.grad(grad_out, (fn_input, sequence_input, linear_param0, linear_param1), grad_output,
                                       allow_unused=True)
        fn_input.detach_()
        fn_input.requires_grad_(True)
        if not ctx.top:
            ctx.output_list.append(fn_input)
        return grad_out + (None,) * 4


class FixedRevRNN(torch.nn.Module):
    def __init__(self, input_cases, hidden_features, out_features, return_sequences=False, delay=8, depth=1,
                 input_count=0):
        """

        :param input_cases: Input cases/max embedding index (not learned, can be extended)
        :param hidden_features: Base of a square feature matrix.
        :param out_features:
        :param return_sequences:
        :param delay:
        :param depth:
        :param input_count:
        """
        super(FixedRevRNN, self).__init__()
        if input_count <= 0:
            raise UserWarning("No input count given")

        hidden_features = hidden_features ** 2
        self.return_sequences = return_sequences
        self.delay = delay
        self.input_count = input_count

        self.hidden_features = hidden_features

        features_sqrt = int(hidden_features ** 0.5)
        self.linear_param0 = torch.nn.Parameter(torch.zeros((depth, 3 * hidden_features, hidden_features)))
        self.linear_param1 = torch.nn.Parameter(torch.zeros((depth, 3 * hidden_features, hidden_features)))
        self.out_linear = torch.nn.Parameter(torch.randn((1, 2 * hidden_features, out_features)))
        self.embedding = torch.nn.Parameter(torch.randn((input_cases, hidden_features)).mul(0.004))

        for idx in range(depth):
            for sub_idx in range(3):
                torch.nn.init.orthogonal_(
                    self.linear_param0[idx][sub_idx * hidden_features:(1 + sub_idx) * hidden_features])
                torch.nn.init.orthogonal_(
                    self.linear_param1[idx][sub_idx * hidden_features:(1 + sub_idx) * hidden_features])

        hidden_state = torch.randn(1, 2 * features_sqrt, features_sqrt)
        hidden_state[0, :features_sqrt] = hidden_state[0, :features_sqrt].qr().Q
        hidden_state[0, features_sqrt:] = hidden_state[0, features_sqrt:].qr().Q
        self.register_buffer("hidden_state", hidden_state.clone())
        self.depth = depth

    def forward(self, fn_input: torch.Tensor):
        # B, S -> B, S, H, H -> B, S, F
        output_list = []
        batch = fn_input.size(0)
        out = self.hidden_state.expand(batch, -1, -1)
        out.requires_grad_(True)
        output = []
        top = True
        base_seq = seq = self.input_count
        seq += self.delay
        zeros = torch.zeros(1, device=fn_input.device, dtype=fn_input.dtype).expand(batch)
        fn = ReversibleRNNFunction().apply
        for idx in range(base_seq):
            out = fn(out, fn_input[:, idx], self.linear_param0, self.linear_param1, output_list, top, self.depth,
                     self.embedding)
            output.append(out)
            top = False
        for idx in range(base_seq, seq):
            out = fn(out, zeros, self.linear_param0, self.linear_param1, output_list, top, self.depth, self.embedding)
            output.append(out)
            top = False
        out = torch.stack(output[self.delay:], 1).view(batch, base_seq, -1)
        out = torch.bmm(out, self.out_linear.expand(batch, -1, -1))
        return out


class Transpose(torch.nn.Module):
    def forward(self, fn_input: torch.Tensor) -> torch.Tensor:
        return fn_input.transpose(1, 2)
