"""Alibi — model adapters and the budget guard.

Everything here is transport + arithmetic. No prompting strategy lives in this file, and no
provider is allowed to decide anything about truth: adapters return raw parsed JSON and the
deterministic verifier in ground.py decides whether it is trusted.

Network behaviour that is verified by tests in this sandbox (no keys, so real model output is
not testable here): URL/headers/body construction, `format`/`responseSchema` wiring, image
payloads, timeout handling, and the 429/403/5xx → QuotaExceeded/ProviderError classification.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlparse


class ProviderError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


class QuotaExceeded(ProviderError):
    """Raised on 429. The router treats this as 'stop calling this provider today', not as
    a per-request retry — hammering a rate-limited free tier is how a hackathon demo dies."""


class ModelUnavailable(ProviderError):
    """Raised when Ollama/the local runtime is down. The router must fall back, not fail."""


def wants_plain_http(host: str) -> bool:
    """Decide TLS from the address, not from a config field, and honour an explicit scheme.

    Every caller in this product talks to either a loopback runtime (Ollama, LM Studio, llama.cpp, vLLM)
    or an HTTPS cloud API. The first version of `https_connection` was named for the second case and
    therefore spoke TLS to the first — which failed with `SSL: WRONG_VERSION_NUMBER` the moment anyone
    pasted the URL LM Studio displays in its own UI (`http://127.0.0.1:1234/v1`). A copy-paste that
    cannot work is a broken integration, so `http://` now means plain HTTP, and loopback defaults to it.
    """
    h = (host or "").strip()
    if h.lower().startswith("http://"):
        return True
    if h.lower().startswith(("https://", "ssl://")):
        return False
    bare = h.split("://", 1)[-1].split("/", 1)[0]
    return (bare.startswith("127.") or bare.startswith("localhost") or bare.startswith("[::1]")
            or bare.endswith(".localhost") or bare.startswith("0.0.0.0"))


def _post(host: str, path: str, body: bytes, headers: dict[str, str], timeout: float,
          insecure: bool = False) -> tuple[int, dict, bytes]:
    plain = wants_plain_http(host)
    ctx = (ssl._create_unverified_context() if (insecure or host.endswith(".localhost"))
           else None if plain else ssl.create_default_context())
    try:
        conn = https_connection(host, timeout, ctx, plain=plain)
        conn.request("POST", path, body=body, headers={**headers, "content-length": str(len(body))})
        r = conn.getresponse()
        raw = r.read()
        status, hdrs = r.status, {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        return status, hdrs, raw
    except (socket.timeout, TimeoutError) as e:
        raise ProviderError(f"timeout after {timeout}s on {host}{path}") from e
    except (ConnectionRefusedError, socket.gaierror, OSError) as e:
        raise ModelUnavailable(f"cannot reach {host}: {e}") from e


def https_connection(host: str, timeout: float, ctx, plain: bool = False):
    """`plain=True` returns an HTTPConnection; the name is kept because three call sites use it and a
    rename that touches a network layer is exactly the kind of change that hides a bug."""
    h = host.split("://", 1)[-1].split("/", 1)[0] if "://" in host else host
    port = None
    if ":" in h and not h.startswith("["):
        h, port = h.rsplit(":", 1)
        port = int(port)
    if plain:
        return (http.client.HTTPConnection(h, port, timeout=timeout) if port
                else http.client.HTTPConnection(h, timeout=timeout))
    return (http.client.HTTPSConnection(h, port, timeout=timeout, context=ctx) if port
            else http.client.HTTPSConnection(h, timeout=timeout, context=ctx))


def _get(host: str, path: str, headers: dict[str, str], timeout: float) -> tuple[int, dict, bytes]:
    """GET, for liveness/model enumeration. Shares `_post`'s error typing so a dead server is
    `ModelUnavailable` (fall back) and a broken server is `ProviderError` (report), never a crash."""
    plain = wants_plain_http(host)
    ctx = None if plain else ssl.create_default_context()
    try:
        conn = https_connection(host, timeout, ctx, plain=plain)
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        status, hdrs2 = r.status, {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        return status, hdrs2, raw
    except (socket.timeout, TimeoutError) as e:
        raise ProviderError(f"timeout after {timeout}s on {host}{path}") from e
    except (ConnectionRefusedError, socket.gaierror, OSError) as e:
        raise ModelUnavailable(f"cannot reach {host}: {e}") from e


def _classify(status: int, payload, headers):
    """(status, parsed, raw) on success; *raises* ProviderError/QuotaExceeded on failure.
    429 is a *typed* failure because the correct reaction is 'stop for the day', not 'retry
    now' — free tiers reset at a wall-clock boundary, not after your backoff window.

    `payload`/`headers` may arrive in either order, and do: `_post` and `_get` both return
    (status, headers, body) while this signature read (status, body, headers) and every caller unpacked
    the tuple with `*`. Harmless while the server answered with JSON; a `TypeError: the JSON object must
    be str, bytes or bytearray, not dict` the moment it answered with anything else (an HTML 502 from a
    proxy in front of LM Studio, an empty 204). Normalising by *type* instead of position cannot be
    re-broken by the next caller."""
    body_like = (bytes, bytearray, str)
    if not isinstance(payload, body_like) and isinstance(headers, body_like):
        payload, headers = headers, payload      # positional swap: neither caller could be trusted
    raw, hdrs = payload, (headers if isinstance(headers, dict) else {})
    if status < 400:
        try:
            return status, (json.loads(raw) if raw else {}), raw
        except json.JSONDecodeError:
            return status, {"_raw": raw.decode("utf-8", "replace")}, raw
    ra = hdrs.get("retry-after")
    retry_after = float(ra) if ra and re.fullmatch(r"\d+(\.\d+)?", ra) else None
    detail = (raw[:300].decode("utf-8", "replace") if raw else "")
    # RAISE, never return: the docstring said "raises" while the code handed the exception object back
    # as a tuple element, so a caller doing `st, data, raw = _classify(*_post(...))` died with
    # `TypeError: cannot unpack non-sequence ProviderError` — an HTTP 500 from LM Studio presented
    # itself to the user as a Python bug in our own transport instead of as what it was.
    if status == 429:
        raise QuotaExceeded(f"429 rate limited ({detail[:120]})", 429, retry_after)
    raise ProviderError(f"HTTP {status} {detail}", status, retry_after)


@dataclass
class Raw:
    text: str
    model: str
    provider: str
    ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    json_obj: dict | None = None
    err: str = ""


# --------------------------------------------------------------- Ollama ----

class Ollama:
    """Local runtime. This is the privacy-critical path: chat exports, ERP screenshots and
    photographed notice boards are only ever sent here."""

    def __init__(self, host: str | None = None, model: str = "qwen3.5:4b",
                 fallbacks: tuple[str, ...] = ("phi4-mini", "llama3.2:3b"), timeout: float = 90.0):
        self.host = (host or os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")).replace("http://", "")
        self.model = model
        self.fallbacks = fallbacks
        self.timeout = timeout

    def healthy(self) -> dict:
        st, data, _ = _post(self.host, "/api/tags", b"{}", {"content-type": "application/json"}, 5.0)
        if isinstance(data, dict) and st == 200:
            return {"ok": True, "models": [m.get("name") for m in data.get("models", [])]}
        return {"ok": False}

    def _call(self, model: str, messages: list[dict], schema: dict, opts: dict) -> Raw:
        body = json.dumps({
            "model": model, "messages": messages, "stream": False,
            "format": schema,                    # constrained decoding: schema-shaped output
            "options": {"temperature": 0.0, "num_ctx": opts.get("num_ctx", 8192),
                        "num_predict": opts.get("num_predict", 1200),
                        "mirostat": 0, "seed": 7},
        }).encode()
        st, data, raw = _classify(*_post(self.host, "/api/chat", body,
                                        {"content-type": "application/json"}, self.timeout))
        msg = (data.get("message") or {}) if isinstance(data, dict) else {}
        txt = msg.get("content", "") or ""
        obj = None
        try:
            obj = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            m = re.search(r"\{.*\}", txt, re.S)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    obj = None
        return Raw(txt, model, "ollama", int(data.get("eval_count", 0) or 0),
                   int(data.get("prompt_eval_count", 0) or 0),
                   int(data.get("eval_count", 0) or 0), obj,
                   "" if obj is not None else "unparseable_json")

    def complete(self, system: str, user: str) -> str:
        """Plain text for the evaluation plan-bot baseline. No schema, no budget contract:
        this path exists only to benchmark a general assistant, never in the product."""
        return self.extract(user, {}, system=system, opts={"temperature": 0.7}).text

    def extract(self, text: str, schema: dict, images: list[bytes] | None = None,
                system: str = "", opts: dict | None = None) -> Raw:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{
            "role": "user", "content": text} + ({"images": [
                __import__("base64").b64encode(i).decode() for i in (images or [])]} if images else {})]
        opts = opts or {}
        last: ProviderError | None = None
        for model in (self.model, *self.fallbacks):
            t0 = time.perf_counter()
            try:
                r = self._call(model, msgs, schema, opts)
                r.ms = int((time.perf_counter() - t0) * 1000)
                return r
            except ModelUnavailable as e:
                raise                       # no point trying a model chain against a dead daemon
            except ProviderError as e:
                last = e
        raise last or ProviderError("all local models failed")


# --------------------------------------------------------------- Gemini ------

class Gemini:
    """Cloud. Free tier, `responseSchema` structured output. Contract enforced by us:
    ONLY grounded, redacted claim data is ever sent here — never raw student text."""

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.5-flash-lite",
                 alternates: tuple[str, ...] = ("gemini-2.5-flash",), timeout: float = 60.0):
        self.key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model
        self.alternates = alternates
        self.timeout = timeout
        self.host = "generativelanguage.googleapis.com"

    @staticmethod
    def _schema_for_openai_shape(s: dict) -> dict:
        """Gemini's `responseSchema` subset: no `additionalProperties`, `$defs` inlined,
        `const`→`enum`, formats dropped. Sending a raw JSON Schema is the usual 400."""
        s = json.loads(json.dumps(s))

        def walk(n):
            if isinstance(n, dict):
                n.pop("additionalProperties", None)
                n.pop("$schema", None); n.pop("$id", None)
                if "const" in n:
                    n["enum"] = [n.pop("const")]
                for k in ("minimum", "maximum"):
                    if k in n and isinstance(n[k], float):
                        n[k] = int(n[k]) if float(n[k]).is_integer() else n[k]
                for v in list(n.values()):
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(s)
        defs = s.pop("$defs", None) or s.pop("definitions", None)
        if defs:
            def sub(n):
                if isinstance(n, dict):
                    ref = n.get("$ref", "")
                    if isinstance(ref, str) and ref.startswith("#/$defs/"):
                        return json.loads(json.dumps(defs[ref.split("/")[-1]]), object_hook=lambda d: d)
                    return {k: sub(v) for k, v in n.items()}
                if isinstance(n, list):
                    return [sub(v) for v in n]
                return n
            s = sub(s)
            walk(s)
        return s

    def _call(self, model: str, payload: dict) -> Raw:
        # Fail fast and *loudly* when the key is absent. Without this the request goes out with an
        # empty key, the API answers 400, and the caller sees a transport shape error instead of the
        # truth: "cloud is not configured", which is a configuration state, not an outage.
        if not self.key:
            raise ProviderError("no GEMINI_API_KEY in environment — cloud not configured")
        path = f"/v1beta/models/{model}:generateContent?key={self.key}"
        st, data, raw = _classify(*_post(self.host, path, json.dumps(payload).encode(),
                                        {"content-type": "application/json"}, self.timeout))
        cand = ((data.get("candidates") or [{}])[0])
        parts = (cand.get("content") or {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts)
        meta = data.get("usageMetadata") or {}
        obj = None
        try:
            obj = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            pass
        finish = cand.get("finishReason")
        return Raw(txt, model, "gemini", int(meta.get("totalTokenCount", 0)),
                   int(meta.get("promptTokenCount", 0) or 0),
                   int(meta.get("candidatesTokenCount", 0) or 0), obj,
                   "" if obj is not None else f"finish={finish or 'no_json'}")

    def extract(self, text: str, schema: dict, images: list[bytes] | None = None,
                system: str = "", opts: dict | None = None) -> Raw:
        import base64
        parts = [{"text": text}]
        for img in images or []:
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}})
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "systemInstruction": {"parts": [{"text": system}]} if system else {},
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": (opts or {}).get("max_tokens", 1500),
                                 "responseMimeType": "application/json",
                                 "responseSchema": self._schema_for_openai_shape(schema)},
            "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"}
                               for c in ("HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_HARASSMENT")],
        }
        last: Exception | None = None
        for model in (self.model, *self.alternates):
            try:
                return self._call(model, payload)
            except QuotaExceeded as e:
                last = e                     # alternate model = different quota bucket, worth one try
        raise last or ProviderError("gemini unavailable")

    @staticmethod
    def redact(claims: list[dict], student_name: str = "", max_chars: int = 4000) -> str:
        """Data minimisation at the egress boundary.

        Google's own pricing page marks free-tier content as "used to improve our products",
        so the payload leaving the machine is *grounded claims only* — no raw student text,
        no peer names from the group export, no URLs, no attendance numbers. A test asserts
        that a name appearing in the source never appears in this output. This is the only
        function through which the ledger may reach a cloud prompt.
        """
        lines: list[str] = []
        for c in claims[:24]:
            pred = c.get("predicate") or c.get("p") or ""
            val = c.get("value") or {}
            safe = {k: v for k, v in val.items() if k in ("date", "weight", "days", "time")}
            lines.append(f"{c.get('subject_id','?')} {pred} {json.dumps(safe, sort_keys=True)}")
            quote = (c.get("evidence_span") or "").strip().replace("\n", " ")
            if student_name:
                quote = re.sub(re.escape(student_name), "[STUDENT]", quote, flags=re.I)
            quote = re.sub(r"\b(\d{10,})\b", "[ID]", quote)
            quote = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "[EMAIL]", quote)
            quote = re.sub(r"https?://\S+", "[URL]", quote)
            if len(quote) > 240:
                quote = quote[:240]
            lines.append(f'   quote: "{quote}"')
        return "\n".join(lines)[:max_chars]


# ------------------------------------------------- OpenAI-compatible (Groq/Cerebras/OpenRouter) --

class OpenAICompat:
    """Any /v1/chat/completions server. Cloud ones (Groq, Cerebras, OpenRouter) need a key; a
    loopback server (LM Studio, llama.cpp's own server) does not, and must not be refused for lacking
    one — that refusal is what made LM Studio unusable here while the code already had the transport."""

    def __init__(self, name: str, host: str, model: str, key_env: str, timeout: float = 60.0,
                 require_key: bool = True):
        self.name, self.host, self.model = name, host, model
        self.key_env = key_env
        self.key = os.environ.get(key_env, "")
        self.require_key = require_key
        self.timeout = timeout

    def models(self) -> list[str]:
        """GET /v1/models — the only portable way to ask an OpenAI-compatible server what it has
        loaded. A model that is not listed here is not a failure, it is a wrong `LM_STUDIO_MODEL`."""
        st, data, _raw = _classify(*_get(self.host, "/v1/models",
                                         {"authorization": f"Bearer {self.key}"} if self.key else {},
                                         min(self.timeout, 6.0)))
        rows = data.get("data") if isinstance(data, dict) else None
        return [m.get("id") or m.get("name") or "" for m in (rows or []) if isinstance(m, dict)]

    def extract(self, text: str, schema: dict, images: list[bytes] | None = None,
                system: str = "", opts: dict | None = None) -> Raw:
        import base64
        if self.require_key and not self.key:
            raise ProviderError(f"{self.name}: no API key in ${self.key_env}")
        content = [{"type": "text", "text": text}]
        for img in images or []:
            content.append({"type": "image_url", "image_url":
                            {"url": "data:image/png;base64," + base64.b64encode(img).decode()}})
        payload = {"model": self.model, "temperature": 0, "max_tokens": (opts or {}).get("max_tokens", 1200),
                   "messages": ([{"role": "system", "content": system}] if system else [])
                               + [{"role": "user", "content": content}],
                   "response_format": {"type": "json_schema", "json_schema":
                                       {"name": "claims", "schema": schema, "strict": True}}}
        st, data, raw = _classify(*_post(self.host, "/v1/chat/completions", json.dumps(payload).encode(),
                                        {"content-type": "application/json",
                                         "authorization": f"Bearer {self.key}"}, self.timeout))
        ch = ((data.get("choices") or [{}])[0].get("message") or {})
        txt = ch.get("content", "") or ""
        try:
            obj = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            obj = None
        u = data.get("usage") or {}
        return Raw(txt, self.model, self.name, int(u.get("total_tokens", 0)),
                   int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0), obj,
                   "" if obj is not None else "unparseable_json")


# ------------------------------------------------------------- budgets ------

@dataclass
class Budget:
    """Hard ceilings. When cloud is exhausted the system degrades to LOCAL_ONLY and says so;
    it does not fail, and it does not queue an unbounded pile for tomorrow."""
    day_calls: int = 400
    day_tokens: int = 1_500_000
    per_artifact_calls: int = 24
    per_artifact_ms: int = 90_000
    state: dict = field(default_factory=dict)
    db_path: str | None = None
    clock: object = staticmethod(lambda: date.today().isoformat())

    def _today(self) -> str:
        return self.clock()

    def _usable(self) -> bool:
        # `:memory:` and a not-yet-created file both mean "there is nowhere durable to meter", which is a
        # property of the deployment, not an error. Tests and `alibi.cli --db :memory:` must not die in a
        # budget layer, and a budget that cannot persist must not pretend it has persisted either.
        return bool(self.db_path) and self.db_path != ":memory:" and os.path.exists(self.db_path)

    def load(self) -> None:
        if not self._usable():
            return
        import sqlite3
        try:
            con = sqlite3.connect(self.db_path)
            try:
                row = con.execute("SELECT calls, tokens FROM budget WHERE day=?", (self._today(),)).fetchone()
                if row:
                    self.state = {"calls": row[0], "tokens": row[1], "day": self._today()}
            finally:
                con.close()
        except sqlite3.Error:
            # no `budget` table yet (a database created before budgets existed): start today at zero
            return

    def _save(self) -> None:
        if not self._usable():
            return
        import sqlite3
        try:
            con = sqlite3.connect(self.db_path)
            con.execute("CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, calls INTEGER, tokens INTEGER)")
            con.execute("INSERT INTO budget(day,calls,tokens) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET "
                        "calls=excluded.calls, tokens=excluded.tokens",
                        (self._today(), self.state.get("calls", 0), self.state.get("tokens", 0)))
            con.commit(); con.close()
        except sqlite3.Error:
            # A read-only volume must not turn a successful extraction into a 500. The day's count lives
            # in memory for this process, and /api/health reports egress from the ledger's own rows.
            pass

    def spend(self, r: Raw, artifact_calls: int) -> None:
        if self.state.get("day") != self._today():
            self.state = {"calls": 0, "tokens": 0, "day": self._today()}
        self.state["calls"] = self.state.get("calls", 0) + 1
        self.state["tokens"] = self.state.get("tokens", 0) + r.tokens_in + r.tokens_out
        self.state["ms"] = self.state.get("ms", 0) + r.ms
        self._save()

    def room_for_cloud(self, artifact_calls: int = 0) -> tuple[bool, str]:
        if self.state.get("day") != self._today():
            return True, ""
        if self.state.get("calls", 0) >= self.day_calls:
            return False, f"daily cloud call cap ({self.day_calls}) reached — LOCAL_ONLY"
        if self.state.get("tokens", 0) >= self.day_tokens:
            return False, f"daily token cap ({self.day_tokens:,}) reached — LOCAL_ONLY"
        if artifact_calls >= self.per_artifact_calls:
            return False, f"per-artifact call cap ({self.per_artifact_calls}) — parked, not dropped"
        return True, ""

    def snapshot(self) -> dict:
        return {"day": self.state.get("day"), "calls": self.state.get("calls", 0),
                "tokens": self.state.get("tokens", 0), "cloud_available": self.room_for_cloud()[0],
                "why_not": self.room_for_cloud()[1], "llm_ms": self.state.get("ms", 0)}
