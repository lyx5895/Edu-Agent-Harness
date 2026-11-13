"""Small serialized, OpenAI-compatible Transformers BF16 server."""
import base64
import io
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field
from transformers import AutoProcessor, Qwen3_5ForConditionalGeneration, StoppingCriteria, StoppingCriteriaList

ROOT = Path(__file__).resolve().parent
MODEL = 'omniedu-9b'
lock = threading.Lock()
state = {}
torch.set_num_threads(8)


@asynccontextmanager
async def lifespan(app):
    path = str(ROOT / 'models/Omni-Edu-9B')
    assert torch.cuda.device_count() == 2, 'Expose exactly two GPUs with CUDA_VISIBLE_DEVICES'
    state['processor'] = AutoProcessor.from_pretrained(path, local_files_only=True)
    state['model'] = Qwen3_5ForConditionalGeneration.from_pretrained(
        path, dtype=torch.bfloat16, device_map='balanced',
        max_memory={0: '18GiB', 1: '18GiB'}, attn_implementation='sdpa', local_files_only=True,
    ).eval()
    state['model'].config.use_cache = True
    state['ready'] = True
    print('DEVICE_MAP', state['model'].hf_device_map, flush=True)
    yield
    state.clear()


app = FastAPI(lifespan=lifespan)


class Request(BaseModel):
    model: str = MODEL
    messages: list[dict] = Field(min_length=1, max_length=50)
    max_tokens: int = Field(default=512, ge=1, le=1024)
    temperature: float = Field(default=0, ge=0, le=2)
    stream: bool = False
    chat_template_kwargs: dict = Field(default_factory=dict)


@app.get('/health')
def health():
    return {'ready': bool(state.get('ready')), 'model': MODEL, 'backend': 'transformers', 'dtype': 'bfloat16'}


@app.get('/v1/models')
def models():
    return {'object': 'list', 'data': [{'id': MODEL, 'object': 'model', 'owned_by': 'local'}]}


def prepare_messages(messages):
    result, images = [], 0
    for message in messages:
        role = message.get('role')
        if role not in ('system', 'user', 'assistant'):
            raise ValueError('Unsupported role')
        content = message.get('content', '')
        if isinstance(content, str):
            if len(content) > 20000:
                raise ValueError('Message too long')
            content = [{'type': 'text', 'text': content}]
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if block.get('type') == 'text':
                    blocks.append({'type': 'text', 'text': str(block['text'])[:20000]})
                elif block.get('type') == 'image_url':
                    images += 1
                    url = block['image_url']['url']
                    if images > 1 or role != 'user' or not url.startswith('data:image/') or ';base64,' not in url:
                        raise ValueError('Only one user image as a base64 data URL is supported')
                    if len(url) > 12_000_000:
                        raise ValueError('Image too large')
                    im = Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1], validate=True)))
                    if im.width * im.height > 20_000_000:
                        raise ValueError('Image dimensions too large')
                    im = im.convert('RGB')
                    im.thumbnail((768, 768))
                    blocks.append({'type': 'image', 'image': im})
                else:
                    raise ValueError('Unsupported content block')
            content = blocks
        else:
            raise ValueError('Invalid message content')
        result.append({'role': role, 'content': content})
    return result


@app.post('/v1/chat/completions')
def chat(req: Request):
    if req.model != MODEL or req.stream:
        raise HTTPException(400, 'Use omniedu-9b with stream=false')
    if not state.get('ready'):
        raise HTTPException(503, 'Model is not ready')
    if not lock.acquire(timeout=300):
        raise HTTPException(503, 'Inference queue busy')
    start = time.monotonic()
    first = []

    class FirstToken(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs):
            if not first:
                torch.cuda.synchronize()
                first.append(time.monotonic() - start)
            return False

    try:
        processor, model = state['processor'], state['model']
        messages = prepare_messages(req.messages)
        inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True,
            return_dict=True, return_tensors='pt', enable_thinking=False).to(model.device)
        n = inputs['input_ids'].shape[-1]
        if n + req.max_tokens > 4096:
            raise HTTPException(413, 'Input plus output exceeds 4096 tokens')
        for i in range(2):
            torch.cuda.reset_peak_memory_stats(i)
        with torch.inference_mode():
            kwargs = {'temperature': req.temperature} if req.temperature > 0 else {}
            output = model.generate(**inputs, max_new_tokens=req.max_tokens, do_sample=req.temperature > 0,
                use_cache=True, stopping_criteria=StoppingCriteriaList([FirstToken()]), **kwargs)
        text = processor.batch_decode(output[:, n:], skip_special_tokens=True)[0]
        return {'id': 'chatcmpl-' + uuid.uuid4().hex, 'object': 'chat.completion', 'created': int(time.time()),
            'model': MODEL, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text},
                'finish_reason': 'length' if output.shape[-1] - n >= req.max_tokens else 'stop'}],
            'usage': {'prompt_tokens': n, 'completion_tokens': output.shape[-1] - n, 'total_tokens': output.shape[-1]},
            'metrics': {'ttft_seconds': first[0] if first else None, 'seconds': time.monotonic() - start,
                'peak_allocated_gib': [round(torch.cuda.max_memory_allocated(i) / 2**30, 3) for i in range(2)]}}
    except HTTPException:
        raise
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        raise HTTPException(503, 'GPU memory exhausted; retry with a shorter input')
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))
    finally:
        lock.release()
