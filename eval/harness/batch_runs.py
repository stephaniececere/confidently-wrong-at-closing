"""Batch backend for run_eval.py: the same requests the live backends send, through Anthropic's Message Batches API
or OpenRouter's Batch API, at about half the price, with results within 24 hours (usually minutes).

Every batch ID is written to <run folder>/batches.json the moment the batch is accepted, before anything else
happens, so a stopped or crashed run never submits the same trials twice: rerunning the same command picks up the
batches already in flight, waits for them, and submits only trials that are neither logged nor in a batch.
"""
import json
import os
import time
import urllib.error
import urllib.request

OR_BATCHES = "https://openrouter.ai/api/v1/batches"
POLL_SECONDS = 60
FIRST_PARTY = ("anthropic/", "google/", "openai/", "x-ai/")  # served only by their own lab (or its cloud partners)


def _or(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenRouter {e.code}: {e.read().decode(errors='replace')[:500]}") from None


def openrouter_batch_models():
    """Model slugs that have a batch variant on OpenRouter (the public models list, no key needed)."""
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=60) as r:
        ids = {m["id"] for m in json.loads(r.read())["data"]}
    return {i[: -len(":batch")] for i in ids if i.endswith(":batch")}


def _apportion_cost(model, status, recs):
    """OpenRouter reports a batch's cost only for the whole batch. When the per-request bodies carry none, split the
    billed total across the trials by each one's token cost at the model's batch prices, so the trial costs sum to
    exactly what was charged; those trials are marked cost_estimated."""
    total = (status.get("usage") or {}).get("cost")
    missing = [r for r in recs if r.get("cost_usd") is None]
    if not missing or total is None:
        return
    pin = pout = 1.0
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=60) as r:
            p = next(m["pricing"] for m in json.loads(r.read())["data"] if m["id"] == f"{model}:batch")
        pin, pout = float(p["prompt"]), float(p["completion"])
    except Exception:  # without prices, weight by tokens alone (output weighted 5x, the usual price ratio)
        pin, pout = 1.0, 5.0
    known = sum(r["cost_usd"] for r in recs if r.get("cost_usd") is not None)
    weights = [(r.get("tokens_in") or 0) * pin + (r.get("tokens_out") or 0) * pout for r in missing]
    for r, w in zip(missing, weights):
        r["cost_usd"] = round((total - known) * w / (sum(weights) or 1), 6)
        r["cost_estimated"] = True


class State:
    """batches.json: one entry per submitted batch, {id, api, model, items: {custom_id: [task, model, trial]}, collected}."""

    def __init__(self, path):
        self.path = path
        self.batches = json.loads(path.read_text()) if path.exists() else []

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.batches, indent=1))
        tmp.replace(self.path)

    def in_flight(self):
        """(task, model, trial) keys sitting in a batch whose results haven't been collected yet."""
        return {tuple(v) for b in self.batches if not b["collected"] for v in b["items"].values()}


def submit(state, api, model, jobs, build):
    """Submit one batch for one model. build(job) returns the request body. Records the batch before returning."""
    items = {f"r{n}": (t["id"], m, i) for n, (t, m, i) in enumerate(jobs)}
    by_id = {f"r{n}": job for n, job in enumerate(jobs)}
    if api == "anthropic":
        import anthropic
        reqs = [{"custom_id": cid, "params": build(by_id[cid])} for cid in items]
        bid = anthropic.Anthropic(max_retries=5).messages.batches.create(requests=reqs).id
    else:
        bodies = {cid: build(by_id[cid]) for cid in items}
        provider = next(iter(bodies.values())).get("provider")
        for b in bodies.values():  # the batch carries model and provider once, at the top level
            b.pop("model", None)
            b.pop("provider", None)
        top = {"endpoint": "/v1/chat/completions", "model": model}  # these keys must come before "requests"
        if provider:
            top["provider"] = provider
        top["requests"] = [{"custom_id": cid, "body": body} for cid, body in bodies.items()]
        try:
            bid = _or("POST", OR_BATCHES, top)["id"]
        except RuntimeError as e:
            # A lab's own model is the same model on any host, so a pin that has no batch endpoint is dropped. Open-weight
            # models keep theirs: their hosts serve different quantizations, which would change the answers.
            if not (provider and "404" in str(e) and model.startswith(FIRST_PARTY)):
                raise
            print(f"{model}: pinned host {provider['only']} has no batch endpoint; submitting unpinned", flush=True)
            del top["provider"]
            top["requests"] = top.pop("requests")
            bid = _or("POST", OR_BATCHES, top)["id"]
    state.batches.append({"id": bid, "api": api, "model": model, "items": items, "collected": False,
                          "submitted": time.strftime("%Y-%m-%dT%H:%M:%S")})
    state.save()
    print(f"batch {bid} submitted: {model}, {len(items)} trials", flush=True)
    return bid


def wait(state):
    """Poll every uncollected batch until each has ended. Returns {batch id: final status object}."""
    final = {}
    while True:
        for b in state.batches:
            if b["collected"] or b["id"] in final:
                continue
            if b["api"] == "anthropic":
                import anthropic
                s = anthropic.Anthropic(max_retries=5).messages.batches.retrieve(b["id"])
                ended, counts = s.processing_status == "ended", s.request_counts
                line = f"{s.processing_status} (done {counts.succeeded}, errored {counts.errored}, processing {counts.processing})"
            else:
                try:
                    s = _or("GET", f"{OR_BATCHES}/{b['id']}")
                except RuntimeError as e:
                    print(f"batch {b['id']} poll failed ({e}); retrying", flush=True)
                    continue
                st = s.get("status")
                ended = st in ("completed", "failed", "expired", "cancelled") and (st != "completed" or s.get("results") is not None)
                line = f"{st} {s.get('request_counts')}"
            print(f"{time.strftime('%H:%M:%S')} batch {b['id']} ({b['model']}): {line}", flush=True)
            if ended:
                final[b["id"]] = s
        if all(b["collected"] or b["id"] in final for b in state.batches):
            return final
        time.sleep(POLL_SECONDS)


def results(b, status, to_record):
    """Yield (key, record or exception) for every request in an ended batch. to_record parses one success."""
    seen = set()
    if b["api"] == "anthropic":
        import anthropic
        for r in anthropic.Anthropic(max_retries=5).messages.batches.results(b["id"]):
            seen.add(r.custom_id)
            key = tuple(b["items"][r.custom_id])
            if r.result.type == "succeeded":
                try:
                    yield key, to_record(r.result.message)
                except Exception as e:
                    yield key, e
            else:
                yield key, RuntimeError(f"batch request {r.result.type}: {getattr(r.result, 'error', '')}")
    else:
        if status.get("status") != "completed":
            err = (status.get("error") or {}).get("message")
            for cid, key in b["items"].items():
                yield tuple(key), RuntimeError(f"batch {status.get('status')}: {err}")
            return
        parsed = []
        for r in status.get("results") or []:
            seen.add(r["custom_id"])
            key = tuple(b["items"][r["custom_id"]])
            if r.get("error"):
                parsed.append((key, RuntimeError(f"batch request error: {r['error']}")))
                continue
            try:
                parsed.append((key, to_record(r["response"]["body"])))
            except Exception as e:
                parsed.append((key, e))
        _apportion_cost(b["model"], status, [rec for _, rec in parsed if isinstance(rec, dict)])
        yield from parsed
    for cid, key in b["items"].items():  # a request the provider never reported is a failure, filled on a rerun
        if cid not in seen:
            yield tuple(key), RuntimeError("no result returned for this request")
