"""Versioned code retrieval. Every candidate is revalidated against the live workspace."""
import ast
import hashlib
import json
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models

from repopilot.checkpoint import checksum
from repopilot.repository import digest

REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def chunks(files):
    result = []
    for path, text in sorted(files.items()):
        lines = text.splitlines(keepends=True)
        boundaries = {1, len(lines) + 1}
        symbols = {}
        if path.endswith(".py"):
            try:
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                        boundaries.update([start, node.end_lineno + 1])
                        symbols[start] = node.name
            except (SyntaxError, ValueError):
                pass  # Partially edited Python still participates in literal retrieval.
        stops = sorted(boundaries)
        for left, right in zip(stops, stops[1:]):
            for start in range(left, right, 30):
                end = min(start + 29, right - 1)
                body = "".join(lines[start - 1:end])
                # Bound embedding input, including exceptionally long single lines.
                for offset in range(0, len(body), 1400):
                    part = body[offset:offset + 1400]
                    if not part.strip():
                        continue
                    chunk = {"path": path, "start": start, "end": end, "offset": offset,
                             "symbol": symbols.get(left, ""), "file_hash": content_hash(text), "text": part}
                    chunk["chunk_hash"] = checksum(chunk)
                    result.append(chunk)
    if len(result) > 2000:
        raise ValueError("Code chunk limit exceeded")
    return result


def terms(text):
    return set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]*|[\u4e00-\u9fff]", text.lower()))


def literal_search(query, candidates, limit=12):
    wanted = terms(query)
    ranked = sorted(candidates, key=lambda c: (
        -len(wanted & terms(c["path"] + " " + c["symbol"])) * 3 - len(wanted & terms(c["text"])),
        c["path"], c["start"], c["offset"]))
    return ranked[:limit]


class LocalEmbedding:
    def __init__(self):
        from fastembed import TextEmbedding
        root = Path(os.getenv("EMBEDDING_MODEL_DIR", "runtime/embedding-model"))
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["revision"] != REVISION or manifest["dimension"] != 384:
            raise ValueError("Unsupported embedding model version")
        for name, expected in manifest["files"].items():
            if Path(name).name != name or content_hash_bytes(root / name) != expected:
                raise ValueError("Embedding model artifact checksum mismatch")
        if not {"model_optimized.onnx", "tokenizer.json"}.issubset(manifest["files"]):
            raise ValueError("Incomplete embedding artifact manifest")
        self.version = checksum({"manifest": manifest, "engine": "fastembed-0.7.4/onnxruntime-1.23.2", "chunker": "ast-lines-v1"})
        self.model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", specific_model_path=str(root),
                                   local_files_only=True, threads=2)

    def documents(self, texts):
        return [v.tolist() for v in self.model.embed(texts, batch_size=32)]

    def query(self, text):
        return list(self.model.query_embed(text))[0].tolist()


def content_hash_bytes(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


embedding_model = lru_cache(maxsize=1)(LocalEmbedding)


class ControlContext:
    def __init__(self, run):
        self.run = run
        self.client = httpx.Client(base_url=os.getenv("CONTROL_API_URL", "http://localhost:3101"), timeout=30,
                                   headers={"authorization": f"Bearer {os.environ['WORKER_TOKEN']}"})

    def call(self, suffix, data=None):
        body = {"workerId": self.run["workerId"], "generation": self.run["generation"], **(data or {})}
        # Stable request IDs make index begin/publish retries safe after a lost response.
        for attempt in range(2):
            try:
                return self.client.post(f"/internal/runs/{self.run['id']}/{suffix}", json=body).raise_for_status().json()
            except (httpx.NetworkError, httpx.TimeoutException):
                if attempt:
                    raise

    def close(self):
        self.client.close()


class CodeIndex:
    def __init__(self, run, base, control, emit, encoder=None, client=None):
        self.run, self.base, self.control, self.emit = run, base, control, emit
        self.encoder = encoder
        self.client = client or QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"), timeout=8)
        self.initialized = set()
        self.base_chunks = chunks(base)

    def ensure(self, files, scope):
        encoder = self.encoder or embedding_model()
        self.encoder = encoder
        parts = self.base_chunks if scope == "base" else chunks(files)
        manifest = checksum([p["chunk_hash"] for p in parts])
        index_id = str(uuid.uuid4())
        point_ids = [str(uuid.uuid5(uuid.UUID(index_id), p["chunk_hash"])) for p in parts]
        build = self.control.call("indexes/begin", {"id": index_id, "scope": scope, "embedding": encoder.version,
            "snapshotHash": digest(files), "manifestHash": manifest, "pointIds": point_ids})
        if build.get("reused"):
            return build
        collection = build["collection"]
        if collection not in self.initialized:
            if not self.client.collection_exists(collection):
                try:
                    self.client.create_collection(collection, vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
                except Exception:
                    if not self.client.collection_exists(collection):
                        raise
            for field in ("index_id", "project_id", "commit", "scope", "embedding", "path", "file_hash"):
                self.client.create_payload_index(collection, field, models.PayloadSchemaType.KEYWORD, wait=True)
            self.initialized.add(collection)
        for start in range(0, len(parts), 32):
            batch = parts[start:start + 32]
            vectors = encoder.documents([p["path"] + " " + p["symbol"] + "\n" + p["text"] for p in batch])
            points = [models.PointStruct(id=point_ids[start + i], vector=vector, payload={**part,
                "index_id": build["id"], "project_id": self.run["projectId"], "commit": self.run["spec"]["commit"],
                "scope": build["head"]["scope"], "embedding": encoder.version,
                "snapshot_hash": build["snapshotHash"], "manifest_hash": manifest})
                for i, (part, vector) in enumerate(zip(batch, vectors, strict=True))]
            self.client.upsert(collection, points, wait=True)
        self.control.call("indexes/publish", {"id": build["id"]})
        self.emit("INDEX_PUBLISHED", {"id": build["id"], "scope": scope, "chunks": len(parts),
                                      "snapshot_hash": build["snapshotHash"], "embedding": encoder.version})
        return build

    def search(self, query, current, limit=12):
        changed = {p for p in self.base.keys() | current.keys() if self.base.get(p) != current.get(p)}
        candidates = chunks(current)
        lexical = literal_search(query, candidates, limit * 2)
        # Never return text from a vector payload: only return hash-validated current chunks.
        current_chunks = {p["chunk_hash"]: p for p in candidates}
        vectors = []
        published = []
        try:
            base = self.ensure(self.base, "base")
            builds = [(base, changed)]
            if changed:
                overlay = {p: current[p] for p in changed if p in current}
                builds.append((self.ensure(overlay, "overlay"), set()))
            query_vector = self.encoder.query(query)
            for build, excluded in builds:
                conditions = {"index_id": build["id"], "project_id": self.run["projectId"],
                              "commit": self.run["spec"]["commit"], "scope": build["head"]["scope"],
                              "embedding": self.encoder.version}
                query_filter = models.Filter(must=[models.FieldCondition(key=k, match=models.MatchValue(value=v))
                                                   for k, v in conditions.items()],
                    must_not=[models.FieldCondition(key="path", match=models.MatchAny(any=sorted(excluded)))] if excluded else [])
                hits = self.client.query_points(build["collection"], query=query_vector, query_filter=query_filter,
                                                limit=limit * 2, with_payload=True).points
                for hit in hits:
                    payload = hit.payload or {}
                    valid = current_chunks.get(payload.get("chunk_hash"))
                    if (valid and all(payload.get(k) == v for k, v in conditions.items())
                            and payload.get("path") not in excluded and payload.get("file_hash") == valid["file_hash"]):
                        vectors.append(valid)
                published.append(build["id"])
        except (Exception,) as error:
            # Control-plane ownership failures must abort; infrastructure failures use current literal evidence.
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
                raise
            self.emit("INDEX_FALLBACK", {"reason": type(error).__name__, "strategy": "current-workspace-literal"})
        scores = {}
        for ranked in (lexical, vectors):
            for rank, part in enumerate(ranked):
                scores[part["chunk_hash"]] = scores.get(part["chunk_hash"], 0) + 1 / (60 + rank + 1)
        selected = sorted(scores, key=lambda k: (-scores[k], k))[:limit]
        return [current_chunks[key] for key in selected], {"indexes": published, "changed_paths": sorted(changed),
                                                          "strategy": "fused" if vectors else "literal"}

    def close(self):
        self.client.close()
