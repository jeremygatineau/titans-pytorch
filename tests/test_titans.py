from contextlib import contextmanager

import torch
from torch import nn

import pytest
from titans_pytorch import NeuralMemory
from titans_pytorch.mac_transformer import flex_attention, SegmentedAttention, MemoryAsContextTransformer

# functions

def exists(v):
    return v is not None

def diff(x, y):
    return (x - y).abs().amax()

@contextmanager
def torch_default_dtype(dtype):
    prev_dtype = torch.get_default_dtype()
    torch.set_default_dtype(dtype)
    yield
    torch.set_default_dtype(prev_dtype)

# main test

@pytest.mark.parametrize('seq_len', (32, 512, 77))
@pytest.mark.parametrize('silu', (False, True))
@pytest.mark.parametrize('chunk_size, attn_pool_chunks', ((64, True), (64, False), (1, False)))
@pytest.mark.parametrize('momentum', (False, True))
@pytest.mark.parametrize('qk_rmsnorm', (False, True))
@pytest.mark.parametrize('max_grad_norm', (None, 2.))
@pytest.mark.parametrize('per_parameter_lr_modulation', (False, True))
def test_titans(
    seq_len,
    silu,
    attn_pool_chunks,
    chunk_size,
    momentum,
    qk_rmsnorm,
    max_grad_norm,
    per_parameter_lr_modulation
):
    mem = NeuralMemory(
        dim = 384,
        chunk_size = chunk_size,
        activation = nn.SiLU() if silu else None,
        attn_pool_chunks = attn_pool_chunks,
        max_grad_norm = max_grad_norm,
        momentum = momentum,
        qk_rmsnorm = qk_rmsnorm,
        per_parameter_lr_modulation = per_parameter_lr_modulation,
    )

    seq = torch.randn(2, seq_len, 384)
    retrieved, _ = mem(seq)

    assert seq.shape == retrieved.shape

def test_titans_attn_memory():
    from titans_pytorch.memory_models import MemoryAttention

    mem = NeuralMemory(
        dim = 384,
        chunk_size = 64,
        model = MemoryAttention(
            dim = 384
        )
    )

    seq = torch.randn(2, 1024, 384)
    retrieved, _ = mem(seq)

    assert seq.shape == retrieved.shape

def test_neural_mem_chaining_chunks():
    mem  = NeuralMemory(
        dim = 384,
        dim_head = 64,
        heads = 2,
        chunk_size = 16
    )

    seq = torch.randn(2, 48, 384)

    parallel_retrieved, state = mem(seq)

    seq_first, seq_second, seq_third = seq.split(16, dim = 1)

    first_retrieved, state = mem(seq_first)
    second_retrieved, state = mem(seq_second, state = state)
    third_retrieved, state = mem(seq_third, state = state)

    assert torch.allclose(parallel_retrieved, torch.cat((first_retrieved, second_retrieved, third_retrieved), dim = 1), atol = 1e-5)

def test_neural_mem_chaining_with_weight_residual():
    mem  = NeuralMemory(
        dim = 384,
        dim_head = 64,
        heads = 2,
        chunk_size = 64
    )

    mem2 = NeuralMemory(
        dim = 384,
        dim_head = 64,
        heads = 2,
        chunk_size = 64
    )

    seq = torch.randn(2, 256, 384)

    seq, state = mem(seq)

    parallel_retrieved, _ = mem2(seq, prev_weights = state.updates)

    seq_first, seq_second = seq[:, :128], seq[:, 128:]

    first_retrieved, state1 = mem2(seq_first, prev_weights = state.updates)
    second_retrieved, state2 = mem2(seq_second, state = state1, prev_weights = state.updates)

    assert torch.allclose(parallel_retrieved, torch.cat((first_retrieved, second_retrieved), dim = 1), atol = 1e-6)

def test_neural_mem_chaining_with_batch_size():
    mem  = NeuralMemory(
        dim = 384,
        dim_head = 64,
        heads = 2,
        chunk_size = 16,
        batch_size = 64
    )

    seq = torch.randn(2, 112, 384)

    parallel_retrieved, state = mem(seq)

    seq_first, seq_second, seq_third = seq[:, :16], seq[:, 16:64], seq[:, 64:]

    first_retrieved, state = mem(seq_first)
    second_retrieved, state = mem(seq_second, state = state)
    third_retrieved, state = mem(seq_third, state = state)

    parallel_part_retrieved = torch.cat((first_retrieved, second_retrieved, third_retrieved), dim = 1)

    assert torch.allclose(parallel_retrieved, parallel_part_retrieved, atol = 1e-5)

@pytest.mark.parametrize('seq_len', (1023, 17))
@pytest.mark.parametrize('num_persist_mem_tokens', (0, 16))
@pytest.mark.parametrize('num_longterm_mem_tokens', (0, 16))
@pytest.mark.parametrize('neural_mem_gate_attn_output', (False, True))
@pytest.mark.parametrize('neural_mem_segment_len', (8, 16))
@pytest.mark.parametrize('neural_mem_weight_residual', (False, True))
@pytest.mark.parametrize('neural_mem_batch_size', (None, 64))
def test_mac(
    seq_len,
    num_persist_mem_tokens,
    num_longterm_mem_tokens,
    neural_mem_gate_attn_output,
    neural_mem_segment_len,
    neural_mem_weight_residual,
    neural_mem_batch_size,
):
    transformer = MemoryAsContextTransformer(
        num_tokens = 256,
        dim = 256,
        depth = 2,
        num_persist_mem_tokens = num_persist_mem_tokens,
        num_longterm_mem_tokens = num_longterm_mem_tokens,
        segment_len = 128,
        neural_mem_gate_attn_output = neural_mem_gate_attn_output,
        neural_memory_segment_len = neural_mem_segment_len,
        neural_memory_batch_size = neural_mem_batch_size,
        neural_mem_weight_residual = neural_mem_weight_residual
    )

    x = torch.randint(0, 256, (1, seq_len))

    logits = transformer(x)
    assert logits.shape == (1, seq_len, 256)

@pytest.mark.parametrize('sliding', (False, True))
@pytest.mark.parametrize('mem_layers', ((), None))
@pytest.mark.parametrize('longterm_mems', (0, 4, 16))
@pytest.mark.parametrize('prompt_len', (4, 16))
@torch_default_dtype(torch.float64)
def test_mac_sampling(
    sliding,
    mem_layers,
    longterm_mems,
    prompt_len
):
    transformer = MemoryAsContextTransformer(
        num_tokens = 256,
        dim = 256,
        depth = 4,
        segment_len = 32,
        num_persist_mem_tokens = 4,
        num_longterm_mem_tokens = longterm_mems,
        sliding_window_attn = sliding,
        neural_memory_layers = mem_layers,
        neural_mem_gate_attn_output = False
    )

    ids = torch.randint(0, 256, (1, 1023))

    # after much training

    prompt = ids[:, :prompt_len]

    sampled = transformer.sample(prompt, 53, use_cache = False, temperature = 0.)
    sampled_with_cache = transformer.sample(prompt, 53, use_cache = True, temperature = 0.)

    assert torch.allclose(sampled, sampled_with_cache)

@pytest.mark.parametrize('seq_len', (2, 64, 256))
@pytest.mark.parametrize('prompt_len', (0, 65))
@pytest.mark.parametrize('mem_chunk_size', (2, 32, 64))
@torch_default_dtype(torch.float64)
def test_neural_mem_inference(
    seq_len,
    prompt_len,
    mem_chunk_size
):

    mem = NeuralMemory(
        dim = 384,
        chunk_size = mem_chunk_size,
    )

    seq = torch.randn(2, seq_len, 384)
    parallel_retrieved, _ = mem(seq)

    assert seq.shape == parallel_retrieved.shape

    state = None
    sequential_retrieved = []

    # test initial parallel prompt

    test_parallel_prompt = prompt_len > 0 and prompt_len < seq_len

    if test_parallel_prompt:
        prompt, seq = seq[:, :prompt_len], seq[:, prompt_len:]
        retrieved_prompt, state = mem(prompt)
        sequential_retrieved.append(retrieved_prompt)

    # sequential inference

    for token in seq.unbind(dim = 1):

        one_retrieved, state = mem.forward(
            token,
            state = state,
        )

        sequential_retrieved.append(one_retrieved)

    sequential_retrieved = torch.cat(sequential_retrieved, dim = -2)

    assert torch.allclose(parallel_retrieved, sequential_retrieved, atol = 1e-6)

@pytest.mark.parametrize('seq_len', (1023, 17))
@pytest.mark.parametrize('sliding', (True, False))
def test_flex(
    seq_len,
    sliding
):
    if not (torch.cuda.is_available() and exists(flex_attention)):
        pytest.skip()

    attn = SegmentedAttention(
        dim = 512,
        segment_len = 32,
        num_persist_mem_tokens = 1,
        num_longterm_mem_tokens = 1,
        use_flex_attn = True,
        sliding = sliding
    ).cuda()

    seq = torch.randn(1, seq_len, 512).cuda()

    out_flex, _ = attn(seq)
    out_non_flex, _ = attn(seq, disable_flex_attn = True)

    assert torch.allclose(out_flex, out_non_flex, atol = 1e-5)

@torch_default_dtype(torch.float64)
def test_assoc_scan():
    from titans_pytorch.neural_memory import AssocScan
    torch.set_default_dtype(torch.float64)

    scan = AssocScan()

    seq_len = 128
    mid_point = seq_len // 2

    gates = torch.randn(2, seq_len, 512).sigmoid()
    inputs = torch.randn(2, seq_len, 512)

    output = scan(gates, inputs)

    gates1, gates2 = gates[:, :mid_point], gates[:, mid_point:]
    inputs1, inputs2 = inputs[:, :mid_point], inputs[:, mid_point:]

    first_half = scan(gates1, inputs1)

    second_half = scan(gates2, inputs2, prev = first_half[:, -1])
    assert second_half.shape == inputs2.shape

    assert torch.allclose(output[:, -1], second_half[:, -1], atol = 1e-6)
