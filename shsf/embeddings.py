from dataclasses import asdict, dataclass
import os
from pathlib import Path

from .models import digest, json_text


@dataclass(frozen=True)
class EmbeddingSpec:
    base: str = "allenai/specter2_base"
    base_revision: str = "3447645e1def9117997203454fa4495937bfbd83"
    adapter: str = "allenai/specter2"
    adapter_revision: str = "2081559630a80fc5851d8f798a05ba81e9468089"
    dimensions: int = 768
    max_length: int = 512
    pooling: str = "cls"
    normalize: bool = True
    input_format: str = "title+tokenizer.sep_token+abstract/v1"

    def to_dict(self):
        return asdict(self)

    @property
    def key(self):
        return "specter2-proximity-" + digest(json_text(self.to_dict()))[:24]


class SpecterEncoder:
    def __init__(self, spec, cache_dir, device="cpu"):
        self.spec, self.cache_dir, self.device = spec, Path(cache_dir), device
        self.model = None

    def load(self):
        if self.model is not None:
            return
        os.environ.setdefault("HF_HOME", str(self.cache_dir.resolve()))
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        try:
            import torch
            from adapters import AutoAdapterModel
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("Install the embedding extra: uv pip install -e '.[embedding]'") from exc
        self.tokenizer = AutoTokenizer.from_pretrained(self.spec.base, revision=self.spec.base_revision,
                                                       cache_dir=str(self.cache_dir), trust_remote_code=False)
        self.model = AutoAdapterModel.from_pretrained(self.spec.base, revision=self.spec.base_revision,
                                                      cache_dir=str(self.cache_dir), trust_remote_code=False)
        self.model.load_adapter(self.spec.adapter, source="hf", version=self.spec.adapter_revision,
                                set_active=True, load_as="paper_proximity", cache_dir=str(self.cache_dir))
        self.model.set_active_adapters("paper_proximity")
        if not self.model.active_adapters:
            raise RuntimeError("SPECTER2 proximity adapter was not activated")
        self.model.to(self.device).eval()
        if self.model.config.hidden_size != self.spec.dimensions:
            raise ValueError("Unexpected SPECTER2 hidden dimension")

    def encode(self, papers):
        self.load()
        import torch
        texts = [p["title"] + self.tokenizer.sep_token + p["abstract"] for p in papers]
        inputs = self.tokenizer(texts, padding=True, truncation=True,
                                max_length=self.spec.max_length, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.inference_mode():
            vectors = self.model(**inputs).last_hidden_state[:, 0, :]
            if self.spec.normalize:
                vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        return vectors.cpu().float().tolist()


def run_worker(database, spec, encoder, batches=1, batch_size=8):
    database.register_model(spec)
    result = {"claimed": 0, "completed": 0, "superseded": 0, "failed": 0}
    # Load/download the model before leasing jobs. A long first download must
    # not cause leases to expire and duplicate work in another worker.
    with database.transaction() as tx:
        pending = tx.execute("SELECT 1 FROM embedding_jobs WHERE model_key=? AND status IN ('pending','retry','running') LIMIT 1", (spec.key,)).fetchone()
    if not pending:
        return result
    encoder.load()
    for _ in range(batches):
        jobs = database.claim_embeddings(spec.key, batch_size)
        if not jobs:
            break
        result["claimed"] += len(jobs)
        try:
            vectors = encoder.encode(jobs)
            if len(vectors) != len(jobs):
                raise ValueError("Encoder returned the wrong batch size")
        except Exception as exc:
            for job in jobs:
                database.fail_embedding(job, exc)
            result["failed"] += len(jobs)
            print(json_text({"event": "embedding_error", "error": str(exc)[:1000]}), flush=True)
            break
        for job, vector in zip(jobs, vectors):
            try:
                completed = database.finish_embedding(job, vector, spec.dimensions)
                result["completed" if completed else "superseded"] += 1
            except Exception as exc:
                database.fail_embedding(job, exc)
                result["failed"] += 1
                print(json_text({"event": "embedding_error", "paper": job["arxiv_id"], "error": str(exc)[:1000]}), flush=True)
        print(json_text({"event": "embedding_batch", **result}), flush=True)
    return result
