from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder
import asyncio
import logging
import time
import torch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Cross-Encoder Reranker Service")

MODEL_NAME = "dragonkue/bge-reranker-v2-m3-ko"
BATCH_SIZE = 32
MAX_CONCURRENT_PER_GPU = 50
models: list[CrossEncoder] = []
devices: list[str] = []
_semaphores: list[asyncio.Semaphore] = []
_counter = 0
_request_count = 0  # empty_cache 주기 제어


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_n: int = 10


class RerankResult(BaseModel):
    index: int
    relevance_score: float


class RerankResponse(BaseModel):
    results: list[RerankResult]


@app.on_event("startup")
def load_model():
    global models, devices, _semaphores

    if not torch.cuda.is_available():
        logger.info("No CUDA available, using CPU")
        m = CrossEncoder(MODEL_NAME, device="cpu")
        models.append(m)
        devices.append("cpu")
        _semaphores.append(asyncio.Semaphore(MAX_CONCURRENT_PER_GPU))
        _warmup(0)
        return

    device_count = torch.cuda.device_count()
    if device_count == 0:
        logger.info("No GPU found, using CPU")
        m = CrossEncoder(MODEL_NAME, device="cpu")
        models.append(m)
        devices.append("cpu")
        _semaphores.append(asyncio.Semaphore(MAX_CONCURRENT_PER_GPU))
        _warmup(0)
        return

    for i in range(device_count):
        free, total = torch.cuda.mem_get_info(i)
        name = torch.cuda.get_device_name(i)
        logger.info(f"GPU {i} ({name}): {free // 1024 // 1024}MiB free / {total // 1024 // 1024}MiB total")

    # 할당된 모든 GPU에 모델 로드 (FP16)
    for i in range(device_count):
        device = f"cuda:{i}"
        logger.info(f"Loading model on {device} (FP16)...")
        m = CrossEncoder(MODEL_NAME, device=device)
        m.model.half()  # FP16: 메모리 50% 절감 + 추론 ~30% 가속
        models.append(m)
        devices.append(device)
        _semaphores.append(asyncio.Semaphore(MAX_CONCURRENT_PER_GPU))

    total_concurrent = MAX_CONCURRENT_PER_GPU * len(models)
    logger.info(f"Loaded {len(models)} model(s) in FP16, max concurrent: {total_concurrent}")

    # Warmup: 첫 요청 지연 제거
    for i in range(len(models)):
        _warmup(i)


def _warmup(gpu_index: int):
    """모델 warmup — CUDA 커널 초기화 + JIT 컴파일을 startup에서 미리 수행"""
    logger.info(f"Warming up model on {devices[gpu_index]}...")
    start = time.time()
    dummy_pairs = [["warmup query", "warmup document"]]
    models[gpu_index].predict(dummy_pairs)
    elapsed = round((time.time() - start) * 1000)
    logger.info(f"Warmup completed on {devices[gpu_index]} in {elapsed}ms")


def _predict_sync(gpu_index: int, pairs: list[list[str]]) -> list[float]:
    """특정 GPU에서 추론 실행 (FP16)"""
    global _request_count
    m = models[gpu_index]

    with torch.cuda.amp.autocast(dtype=torch.float16):
        if len(pairs) <= BATCH_SIZE:
            scores = m.predict(pairs).tolist()
        else:
            scores = []
            for i in range(0, len(pairs), BATCH_SIZE):
                batch = pairs[i:i + BATCH_SIZE]
                batch_scores = m.predict(batch).tolist()
                scores.extend(batch_scores)

    # empty_cache는 100 요청마다만 호출 (매번 호출 시 성능 오버헤드)
    _request_count += 1
    if _request_count % 100 == 0:
        torch.cuda.empty_cache()

    return scores


@app.post("/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest):
    global _counter
    pairs = [[request.query, doc] for doc in request.documents]

    # 라운드로빈으로 GPU 선택
    gpu_index = _counter % len(models)
    _counter += 1

    async with _semaphores[gpu_index]:
        loop = asyncio.get_event_loop()
        scores = await loop.run_in_executor(None, _predict_sync, gpu_index, pairs)

    indexed_scores = list(enumerate(scores))
    indexed_scores.sort(key=lambda x: -x[1])
    top_results = indexed_scores[:request.top_n]

    return RerankResponse(
        results=[
            RerankResult(index=idx, relevance_score=score)
            for idx, score in top_results
        ]
    )


@app.get("/health")
def health():
    gpu_info = []
    for i, device in enumerate(devices):
        info = {"device": device, "model": MODEL_NAME, "precision": "fp16"}
        if device.startswith("cuda") and torch.cuda.is_available():
            device_index = int(device.split(":")[1])
            info["device_name"] = torch.cuda.get_device_name(device_index)
            info["memory_allocated_mb"] = round(torch.cuda.memory_allocated(device_index) / 1024 / 1024)
            info["memory_reserved_mb"] = round(torch.cuda.memory_reserved(device_index) / 1024 / 1024)
        gpu_info.append(info)

    return {
        "status": "ok",
        "model": MODEL_NAME,
        "precision": "fp16",
        "gpu_count": len(models),
        "max_concurrent_per_gpu": MAX_CONCURRENT_PER_GPU,
        "max_concurrent_total": MAX_CONCURRENT_PER_GPU * len(models),
        "total_requests": _request_count,
        "gpus": gpu_info,
    }
