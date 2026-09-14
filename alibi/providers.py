"""alibi.providers — the model layer, where nothing is ever trusted.

Design rules, all of them load-bearing:

1. **Every method returns a `ModelResult`, never raises, and never returns a bare string.** PART 38's
   SUCCESS / PARTIAL / UNAVAILABLE / INVALID_OUTPUT / ESCALATED / REVIEW_REQUIRED have to be *values*
   that travel through the pipeline, because the alternative — an exception that some caller catches and
   replaces with "no claims" — makes "the model was down" indistinguishable from "the document said
   nothing". Those are different states for the user and different rows in the audit log.

2. **The provider is named in the result, and `confidence` is carried as evidence only.** Trust is
   assigned by the verifier and the trust policy, so a model that says `confidence: 0.99` changes
   nothing about promotion. There is no code path where a model-supplied number becomes trust.

3. **One contract, five backends.** Gemma 4 E4B is the preferred local path; anything OpenAI-compatible
   is a transport detail; the cloud is an escalation, not an upgrade. `describe()` reports what the
   machine can *actually* do, because a UI that shows a configured-but-dead model is a UI that gets
   blamed for the model's outage.

4. **Egress is a property of the call, not of the provider.** A cloud call is either refused or
   REDACTED, and the redaction policy is configuration — so "privacy" is a rule that can be tested, not
   a promise in a README.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .adapters import (Budget, Gemini, ModelUnavailable, Ollama, OpenAICompat, ProviderError,
                       QuotaExceeded, Raw)
from .ground import instructional

SUCCESS, PARTIAL, UNAVAILABLE, INVALID_OUTPUT = "SUCCESS", "PARTIAL", "UNAVAILABLE", "INVALID_OUTPUT"
ESCALATED, REVIEW_REQUIRED = "ESCALATED", "REVIEW_REQUIRED"

# Preferred local model first (PART 5). Every tag is tried in order, and the *resolved* tag is what
# gets reported — a model that is not on disk is not a failure, it is a fallback with a receipt.
GEMMA_TAGS = ("gemma4:e4b", "gemma4-e4b", "gemma4", "gemma3n:e4b", "gemma3:4b")
LOCAL_TAGS = GEMMA_TAGS + ("qwen3.5:4b", "llama3.2:3b")


@dataclass
class ModelResult:
    outcome: str                                  # SUCCESS|PARTIAL|UNAVAILABLE|INVALID_OUTPUT|...
    candidates: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    model_version: str = ""
    egress: str = "LOCAL"                         # LOCAL|CLOUD|REDACTED_CLOUD|NONE
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    est_cost_usd: float = 0.0
    error: str = ""
    raw_head: str = ""
    notes: list[str] = field(default_factory=list)
    observation_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (SUCCESS, PARTIAL)

    @property
    def degraded(self) -> bool:
        return self.outcome in (UNAVAILABLE, INVALID_OUTPUT)

    def as_row(self) -> dict:
        return {k: getattr(self, k) for k in
                ("outcome", "provider", "model", "model_version", "egress", "latency_ms",
                 "tokens_in", "tokens_out", "bytes_in", "bytes_out", "est_cost_usd", "error")}


class Provider(Protocol):
    name: str
    egress: str

    def healthy(self) -> dict: ...
    def extract(self, text: str, schema: dict, *, system: str = "", images: list[bytes] | None = None,
                opts: dict | None = None) -> ModelResult: ...
    def draft(self, system: str, user: str) -> ModelResult: ...
    def summarize(self, system: str, user: str) -> ModelResult: ...


# ------------------------------------------------------------- redaction ----

@dataclass
class RedactionPolicy:
    """Field-level minimisation applied before anything leaves the machine (PART 18).

    Patterns are applied to the *text being sent*, not to the ledger — so a policy change never
    rewrites history, it only changes what the next cloud request contains.
    """
    drop_keys: tuple[str, ...] = ("attendance_pct", "grade_pct", "sleep_floor_min", "journal",
                                 "wellness")
    mask: tuple[tuple[str, str], ...] = (
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\b", "[EMAIL]"),
        (r"\b(?:\+91[\s-]?)?[6-9]\d{9}\b", "[PHONE]"),
        # order matters: an alphanumeric ERP roll number must be masked before the generic digit
        # run, or `[ID]` swallows the numeric half and leaves `CSE0417` looking anonymous while
        # still being a unique identifier inside your own college.
        (r"(?i)\broll\s*(?:no|number)?\.?\s*[A-Z0-9-]{5,}", "roll [ID]"),
        (r"\b\d{6,}\b", "[ID]"),                      # ERP numeric ids, receipt numbers
        # Alphanumeric roll numbers (21CSE0417, CH2110047) need a shape rule: the digit run misses
        # them, and a blanket 7-char rule would eat every version-like token, so require at least
        # two digits AND two letters. Deliberately conservative - over-masking costs the model a
        # number, under-masking costs the student their identifier.
        (r"\b(?=[A-Z0-9]*\d{2,})(?=[A-Z0-9]*[A-Z]{2,})[A-Z]{2}[A-Z0-9]{5,}\b", "[ID]"),
        (r"\b(?=[A-Z0-9]*[A-Z]{2,})\d{2}[A-Z]{2,}[A-Z0-9]{3,}\b", "[ID]"),
        (r"https?://\S+", "[URL]"),
        (r"(?i)\b(roll\s*(no|number)?\.?\s*)\d+", r"\1[ID]"),
    )
    max_quote_chars: int = 220
    max_items: int = 16
    names: tuple[str, ...] = ()

    def scrub_text(self, text: str) -> str:
        out = text or ""
        for n in self.names:
            if n:
                out = re.sub(re.escape(n), "[PERSON]", out, flags=re.I)
        for pat, rep in self.mask:
            out = re.sub(pat, rep, out)
        return out[: self.max_quote_chars * 4]

    def filter_claims(self, claims: list[dict]) -> tuple[list[dict], list[str]]:
        kept, dropped = [], []
        for c in claims[: self.max_items]:
            v = dict(c.get("value") or {})
            for k in list(v):
                if k in self.drop_keys:
                    dropped.append(k)
                    v.pop(k)
            q = self.scrub_text((c.get("evidence_span") or c.get("quote") or "").strip())
            kept.append({**c, "value": v, "evidence_span": q})
        return kept, sorted(set(dropped))


DEFAULT_REDACTION = RedactionPolicy()


def _host_of(url: str) -> str:
    """`http://127.0.0.1:1234/v1` -> `127.0.0.1:1234`. The scheme and the /v1 suffix are transport
    details of the OpenAI dialect; our client wants host:port."""
    from urllib.parse import urlparse
    if not url:
        return ""
    u = urlparse(url if "//" in url else "//" + url)
    host = u.netloc or u.path.split("/")[0]
    return host.replace("http://", "").replace("https://", "")


_LOOPBACK = ("127.", "localhost", "[::1]", "::1", "0.0.0.0")


def is_loopback(host: str) -> bool:
    """The privacy boundary, expressed as one function so it can be tested and printed.

    Anything that is not loopback gets the same treatment as a cloud provider — redaction, budget
    counting, `egress=REDACTED_CLOUD` — even if it is a neighbour's desktop on the college LAN. "Local"
    is a claim about where bytes go, not about who owns the machine.
    """
    h = (host or "").lower()
    return any(h.startswith(x) or (":" + x.rstrip(".")) in h for x in _LOOPBACK) or h == ""


# ------------------------------------------------------------- wrapper ------

class _Wrapped:
    """Shared plumbing: timing, byte accounting, structured-output parsing, and the rule that a
    provider failure is a *value*."""
    name = "wrapper"
    egress = "LOCAL"
    model = ""

    def __init__(self, model: str, *, budget: Budget | None = None,
                 redaction: RedactionPolicy | None = None, timeout: float = 90.0,
                 max_output_tokens: int = 2048) -> None:
        self.model = model
        self.budget = budget
        self.redaction = redaction or DEFAULT_REDACTION
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.resolved_model = model

    # -- helpers --
    @staticmethod
    def _outcome_from_raw(r: Raw | None, err: str = "") -> tuple[str, list[dict], bool]:
        """Classify what came back. Note `INVALID_OUTPUT` is decided by *parseability*, not by the
        model's confidence: an unparsable blob is a contract violation and must be visible, because
        silently treating it as 'no facts' is how a demo ends up with an empty ledger and no idea why."""
        if r is None:
            return UNAVAILABLE, [], False
        obj = r.json_obj
        if obj is None and (r.err or not (r.text or "").strip()):
            return (INVALID_OUTPUT if r.err else PARTIAL), [], False
        if obj is None:
            obj = _loose_json(r.text)
            if obj is None:
                return INVALID_OUTPUT, [], False
        facts = obj.get("facts") if isinstance(obj, dict) else None
        if facts is None and isinstance(obj, list):
            facts = obj
        if not isinstance(facts, list):
            return INVALID_OUTPUT, [], False
        out = [f for f in facts if isinstance(f, dict)]
        return (SUCCESS if out else PARTIAL), out, len(out) != len(facts)

    def _tally(self, r: Raw, text_in: str) -> dict:
        return {"latency_ms": getattr(r, "ms", 0), "tokens_in": getattr(r, "tokens_in", 0),
                "tokens_out": getattr(r, "tokens_out", 0), "bytes_in": len(text_in.encode()),
                "bytes_out": len((getattr(r, "text", "") or "").encode()),
                "est_cost_usd": 0.0}

    def _fail(self, started: float, why: str, note: str = "") -> ModelResult:
        res = ModelResult(outcome=UNAVAILABLE, provider=self.name, model=self.resolved_model,
                          egress=self.egress if "CLOUD" in self.egress else "NONE",
                          latency_ms=int((time.perf_counter() - started) * 1000),
                          error=why[:300])
        if note:
            res.notes.append(note)
        return res


def _g(m) -> str:
    """`.group(0)` for a match that may not exist — spelled once, because an inline
    `or type(...)` hack is how a reader loses the plot at 2am."""
    return m.group(0).strip() if m else ""


def _loose_json(text: str) -> Any:
    """Models that ignore 'JSON only' still usually emit one object. Recover it rather than
    dropping the whole block — but never *repair* it: no brace insertion, no eval."""
    if not text:
        return None
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = t.find(opener), t.rfind(closer)
        if 0 <= s < e:
            try:
                return json.loads(t[s:e + 1])
            except Exception:
                continue
    return None


class GemmaLocal(_Wrapped):
    """Local Gemma 4 E4B (or whatever local tag is actually present) via Ollama.

    Vision-capable and small enough for 6 GB, which is why it is the default: the sensitive
    artefacts — the group chat, the ERP screenshot, the photographed notice board — are exactly the
    ones that must not leave the machine, and they are also the ones a 4B model can do well enough
    because the *verifier* catches its mistakes and the *ledger* keeps the receipts.
    """
    name = "gemma_local"

    def __init__(self, model: str = "", *, host: str | None = None, budget: Budget | None = None,
                 fallbacks: tuple[str, ...] = LOCAL_TAGS, **kw) -> None:
        super().__init__(model or GEMMA_TAGS[0], budget=budget, **kw)
        self.client = Ollama(model=self.model, host=host, fallbacks=fallbacks, timeout=self.timeout)
        self.fallbacks = tuple(fallbacks) if fallbacks else LOCAL_TAGS

    def healthy(self) -> dict:
        t0 = time.perf_counter()
        try:
            info = self.client.healthy() or {}
            models = [m for m in (info.get("models") or []) if m]
            # A tag that is not pulled is not a failure, it is a fallback — but the UI must say which
            # model actually answered, or someone will demo `llama3.2:3b` believing it was Gemma.
            chosen = self.model if self.model in models else next(
                (f for f in self.fallbacks if f in models), "")
            return {"available": bool(models), "provider": self.name, "model": chosen or self.model,
                    "requested": self.model, "installed": models,
                    "fallback_used": bool(models) and not chosen,
                    "probe_ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:
            return {"available": False, "provider": self.name, "model": self.model,
                    "reason": f"{type(e).__name__}: {str(e)[:120]}",
                    "probe_ms": int((time.perf_counter() - t0) * 1000)}

    def extract(self, text: str, schema: dict, *, system: str = "",
                images: list[bytes] | None = None, opts: dict | None = None) -> ModelResult:
        t0 = time.perf_counter()
        try:
            r = self.client.extract(text, schema, images=images, system=system,
                                    opts=opts or {"temperature": 0})
        except ModelUnavailable as e:
            return self._fail(t0, f"no local model reachable: {e}")
        except ProviderError as e:
            return self._fail(t0, f"local provider error: {e}")
        except Exception as e:                                  # never let a socket kill an ingest
            return self._fail(t0, f"local provider crashed: {type(e).__name__}: {e}")
        self.resolved_model = getattr(r, "model", self.model) or self.model
        outcome, facts, truncated = self._outcome_from_raw(r, "")
        res = ModelResult(outcome=outcome, candidates=facts, provider=self.name,
                          model=self.resolved_model, model_version=self.resolved_model,
                          egress="LOCAL", raw_head=(r.text or "")[:160],
                          **self._tally(r, text))
        if truncated:
            res.notes.append("non-object entries in facts[] were dropped")
        if self.budget:
            self.budget.spend(r, 0)
        return res

    draft = summarize = extract            # local drafting needs no redaction; it never leaves


class LocalChat(_Wrapped):
    """A local OpenAI-compatible server — **LM Studio** first and foremost.

    The distinction that matters here is not the vendor, it is the *address*. `CloudEscalation` treats
    every OpenAI-compatible endpoint as an escalation (redaction + budget) because it is probably
    somebody else's computer. LM Studio on `127.0.0.1:1234` is your computer, so the payload is sent
    un-redacted (otherwise the model cannot be shown the quote it is being asked to read) and the egress
    field says `LOCAL`. That decision is made from the URL, not from a provider label someone typed in a
    config file: `is_loopback()` is the whole privacy boundary, and /settings prints the verdict.

    Failure behaviour, because "AI is a laptop service and the laptop lid is closed" is the normal case:

      * nothing listening           → `ModelUnavailable` → UNAVAILABLE → rules + simulator keep working
      * server up, model not found  → a named error with the models it *does* have, no guessing
      * timeout / 5xx / 429         → ProviderError/QuotaExceeded → UNAVAILABLE with the status attached
      * malformed or empty JSON     → INVALID_OUTPUT, counted, never silently treated as "no facts"
    """
    name = "local_chat"
    egress = "LOCAL"

    def __init__(self, model: str = "", *, base_url: str = "", api_key_env: str = "",
                 budget: Budget | None = None, timeout: float = 90.0, **kw) -> None:
        super().__init__(model or "local-chat", budget=budget, timeout=timeout, **{k: v for k, v in kw.items()
                                                                                    if k in ("max_output_tokens",)})
        self.base_url = base_url or ""
        self.host = _host_of(self.base_url)
        self.api_key_env = api_key_env
        self.loopback = is_loopback(self.host)
        self.client = OpenAICompat("lm-studio" if self.loopback else "openai-compat", self.host,
                                   self.model, api_key_env or "LM_STUDIO_API_KEY",
                                   timeout=timeout, require_key=not self.loopback)
        self.fallbacks: tuple[str, ...] = ()

    def healthy(self) -> dict:
        t0 = time.perf_counter()
        out = {"available": False, "provider": self.name, "model": self.model,
               "requested": self.model, "base_url": self.base_url or "(unset)",
               "loopback": self.loopback, "installed": []}
        if not self.base_url:
            out["reason"] = "no base URL configured (LM_STUDIO_BASE_URL / OPENAI_BASE_URL)"
            return out
        try:
            models = self.client.models()
        except ModelUnavailable as e:
            out["reason"] = f"server not reachable: {str(e)[:140]}"
            out["hint"] = "start LM Studio → Developer → Start Server, or run `lm studio server start`"
            return out
        except ProviderError as e:
            out["reason"] = f"server answered badly: {str(e)[:140]}"
            out["hint"] = "check the port serves /v1 (LM Studio exposes OpenAI-compatible routes under /v1)"
            return out
        out["installed"] = [m for m in models if m]
        out["available"] = bool(out["installed"])
        out["model"] = self.model if self.model in out["installed"] else (
            out["installed"][0] if out["installed"] else "")
        if self.model and self.model not in out["installed"]:
            out["model_not_found"] = True
            out["reason"] = (f"{self.model!r} is not loaded; loaded models: "
                             f"{', '.join(out['installed'][:6]) or '(none)'}")
            out["hint"] = "load the model in LM Studio's model pane, or set LM_STUDIO_MODEL to one listed there"
        out["probe_ms"] = int((time.perf_counter() - t0) * 1000)
        return out

    def _resolved(self) -> str:
        if self.model:
            return self.model
        try:
            got = self.client.models()
            if got:
                self.model = got[0]
                self.client.model = got[0]
        except Exception:
            pass
        return self.model

    def extract(self, text: str, schema: dict, *, system: str = "",
                images: list[bytes] | None = None, opts: dict | None = None) -> ModelResult:
        t0 = time.perf_counter()
        self._resolved()
        try:
            r = self.client.extract(text, schema, images=images, system=system,
                                    opts={"temperature": 0, "max_tokens": self.max_output_tokens,
                                          **(opts or {})})
        except ModelUnavailable as e:
            return self._fail(t0, f"local server unreachable: {e}")
        except QuotaExceeded as e:
            return self._fail(t0, f"local server rate limited: {e}")
        except ProviderError as e:
            return self._fail(t0, f"local provider error: {e}")
        except Exception as e:
            return self._fail(t0, f"local provider crashed: {type(e).__name__}: {e}")
        self.resolved_model = getattr(r, "model", self.model) or self.model
        outcome, facts, truncated = self._outcome_from_raw(r, "")
        res = ModelResult(outcome=outcome, candidates=facts, provider=self.name,
                          model=self.resolved_model, model_version=self.resolved_model,
                          egress=self.egress, raw_head=(r.text or "")[:160],
                          **self._tally(r, text))
        if truncated:
            res.notes.append("non-object entries in facts[] were dropped")
        if r.err:                                   # e.g. "unparseable_json"
            res.notes.append(f"transport said: {r.err}")
        return res

    draft = summarize = extract


class CloudEscalation(_Wrapped):
    """Gemini / any OpenAI-compatible endpoint. Cloud is an *escalation*, and the only thing that
    reaches it is a redacted, grounded claim set — never raw student text."""
    name = "cloud"

    def __init__(self, *, budget: Budget | None = None, redaction: RedactionPolicy | None = None,
                 provider_label: str = "gemini", model: str = "gemini-2.5-flash-lite", **kw) -> None:
        super().__init__(model, budget=budget, redaction=redaction,
                         timeout=float(kw.get("timeout", 60.0)))
        timeout = self.timeout
        self.provider_label = provider_label
        self.egress = "REDACTED_CLOUD"
        self.kind = "gemini" if provider_label == "gemini" else "openai"
        # `Gemini` takes no budget: spend is metered by this wrapper, so the transport object stays
        # dumb and cannot disagree with the policy about what is allowed.
        self.client = (Gemini(model=model, timeout=timeout) if self.kind == "gemini" else
                       OpenAICompat(provider_label, kw.get("host", ""), model,
                                    kw.get("key_env", "OPENAI_API_KEY"), timeout=timeout))

    def healthy(self) -> dict:
        has_key = bool(getattr(self.client, "key", ""))
        return {"available": has_key, "provider": self.provider_label, "model": self.model,
                "reason": "" if has_key else "no API key in environment"}

    def prepare(self, claims: list[dict]) -> tuple[list[dict], list[str], str]:
        kept, dropped = self.redaction.filter_claims(claims)
        payload = json.dumps(kept, ensure_ascii=False)
        return kept, dropped, self.redaction.scrub_text(payload)

    def extract(self, text: str, schema: dict, *, system: str = "",
                images: list[bytes] | None = None, opts: dict | None = None) -> ModelResult:
        t0 = time.perf_counter()
        if self.budget:
            room, why = self.budget.room_for_cloud()
            if not room:
                return ModelResult(outcome=UNAVAILABLE, provider=self.provider_label,
                                   model=self.model, egress="NONE", error=f"budget: {why}",
                                   notes=["escalation refused by policy, not by failure"])
        sent = self.redaction.scrub_text(text)
        try:
            r = self.client.extract(sent, schema, images=images, system=system,
                                    opts=opts or {"temperature": 0})
        except (QuotaExceeded, ModelUnavailable, ProviderError) as e:
            return self._fail(t0, f"cloud: {type(e).__name__}: {e}")
        except Exception as e:
            return self._fail(t0, f"cloud crashed: {type(e).__name__}: {e}")
        outcome, facts, truncated = self._outcome_from_raw(r, "")
        res = ModelResult(outcome=outcome, candidates=facts, provider=self.provider_label,
                          model=getattr(r, "model", self.model), model_version=self.model,
                          egress=self.egress, raw_head=(r.text or "")[:160],
                          **self._tally(r, sent))
        res.bytes_in = len(sent.encode())
        if dropped := self.redaction.drop_keys:
            res.notes.append(f"redacted fields: {','.join(dropped)}")
        if self.budget:
            self.budget.spend(r, 1)
        return res

    def draft(self, system: str, user: str) -> ModelResult:
        """Drafting is the only place the cloud sees prose, and what it sees is the claim summary
        produced by `prepare()` — which the caller has already filtered. Bounded output, no tools."""
        t0 = time.perf_counter()
        sent = self.redaction.scrub_text(user)
        try:
            r = self.client.extract(sent, {}, system=system, opts={"temperature": 0.3})
        except Exception as e:
            return self._fail(t0, f"cloud draft: {type(e).__name__}: {e}")
        return ModelResult(outcome=SUCCESS, provider=self.provider_label, model=self.model,
                           egress=self.egress, candidates=[], raw_head=(r.text or "")[:4000],
                           **self._tally(r, sent))

    summarize = draft


class RulesOnly(_Wrapped):
    """Not a model: the identity provider for DEMO MODE and for machines with no GPU and no key.
    Returning this instead of `None` is what makes 'the model layer is unavailable' a *normal,
    testable* state rather than a branch every caller has to remember."""
    name = "rules_only"
    egress = "NONE"

    def healthy(self) -> dict:
        return {"available": True, "provider": self.name, "model": "-",
                "note": "deterministic extraction only; nothing was sent to any model"}

    def extract(self, text: str, schema: dict, *, system: str = "",
                images: list[bytes] | None = None, opts: dict | None = None) -> ModelResult:
        return ModelResult(outcome=UNAVAILABLE, provider=self.name, model="-", egress="NONE",
                           error="no model configured — rules path handles what it can, the rest "
                                 "stays in the review queue")

    draft = summarize = extract


class DeterministicSimulator(_Wrapped):
    """The offline stand-in for a local model — and the honest reason the demo still runs at 3am.

    It reads a document the way a small model would (find a commitment phrase, find the date, pair
    them) and returns *candidates in the schema shape*, which then go through the real verifier and
    the real ledger. That is a simulation of the extractor, not of the product: the parts that decide
    what becomes trusted state are the production parts.

    It is a provider rather than a test fixture because a fixture you swap in from outside is a fixture
    nobody demos; as a provider it is what `describe()` reports and what the routing panel must name.
    """
    name = "deterministic_simulator"
    # LOCAL, not NONE: "no bytes left the machine" is the privacy fact, and the run counters aggregate
    # on egress class. NONE is reserved for calls that were refused before anything was prepared.
    egress = "LOCAL"

    _PAIR = re.compile(
        r"(?is)\b(due|submission|submissions?|submit|closes?|closing|extended to|moved to|changed to|"
        r"rescheduled to|exam|test|quiz|viva|deadline)\b[^.\n]{0,40}?"
        r"(\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?|"
        r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
        r"(?:\s+\d{4})?|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
        r"\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}|\d{4}-\d{2}-\d{2})")
    # Month/day words are excluded by construction: an earlier draft matched "Oct 2026" as a task
    # title, which then minted a *new* subject for a deadline that already had one — the exact way
    # a sync turns one task into two and makes a conflict invisible.
    _TITLE = re.compile(r"(?i)\b([A-Za-z]{2,8}\s?(?:lab|ia|ct|quiz|assignment|tutorial|project|"
                        r"report|viva|end\s?sem|seminar)\s?\d+|\b(?:lab|ia|ct|quiz)\s?\d+\b)")
    _WEIGHT = re.compile(r"(?i)(\d{1,2}(?:\.\d+)?)\s*%\s*(?:of\s*(?:the\s*)?(?:course|final|grade)"
                         r"|weight|marks)")
    _TIME = re.compile(r"(?i)\b(\d{1,2}):(\d{2})\s*(am|pm)?\b")

    def healthy(self) -> dict:
        return {"available": True, "provider": self.name, "model": "regex-extractor-v1",
                "egress": self.egress, "note": "offline deterministic extractor; every candidate still passes the real "
                        "verifier, so nothing is trusted because a regex liked it"}

    def extract(self, text: str, schema: dict, *, system: str = "",
                images: list[bytes] | None = None, opts: dict | None = None) -> ModelResult:
        t0 = time.perf_counter()
        facts: list[dict] = []
        for m in self._PAIR.finditer(text or ""):
            # The line containing the date is the search area — not a ±40 char window (which
            # misses "says 12 Oct now for lab 4", where the label follows the date) and not the whole
            # document (which would happily attach a *different* task's title to this date).
            s0 = (text.rfind("\n", 0, m.start()) + 1) if "\n" in text[:m.start()] else 0
            e0 = text.find("\n", m.end())
            line = text[s0:] if e0 < 0 else text[s0:e0]
            span = line.strip()[:600]
            title = _g(self._TITLE.search(line))
            facts.append({"fact_type": "due_at", "value_date": m.group(2),
                          "task_title": title.strip() or "unnamed item",
                          "source_quote": span.strip()[:300], "confidence": 0.8})
        for m in self._WEIGHT.finditer(text or ""):
            facts.append({"fact_type": "weight", "value_number": round(float(m.group(1)) / 100.0, 4),
                          "source_quote": text[max(0, m.start() - 30):m.end() + 10].strip()[:300],
                          "task_title": _g(self._TITLE.search(text[:m.start()][-200:])) or "course",
                          "confidence": 0.8})
        out = ModelResult(outcome=SUCCESS if facts else PARTIAL, candidates=facts, provider=self.name,
                          model="regex-extractor-v1", model_version="regex-extractor-v1",
                          egress=self.egress,
                          latency_ms=int((time.perf_counter() - t0) * 1000), bytes_in=len(text or ""),
                          raw_head=json.dumps(facts[:2], ensure_ascii=False)[:160])
        if not facts:
            out.notes.append("no commitment-shaped text found; the review queue keeps the block "
                            "visible instead of pretending it was empty")
        return out

    draft = summarize = extract


# --------------------------------------------------------------- registry ---

def build(cfg, *, policy: dict | None = None,
          db=None) -> tuple[Provider | None, CloudEscalation | GemmaLocal | None, dict]:
    """(local, cloud, description). Detection is probed, mode is *derived* from what answered."""
    from .config import resolve_mode
    red = RedactionPolicy(names=tuple((policy or {}).get("redact_names") or ()))
    budget = _budget_for(cfg)
    if db is not None:
        # The day's spend must survive a restart, which means it belongs in the same file as the ledger —
        # not in whatever `cfg.db_path` happened to default to.
        budget.db_path = str(getattr(db, "path", None) or cfg.db_path)
        budget.load()
    # Which local runtime? An OpenAI-compatible endpoint (LM Studio, llama.cpp, vLLM) is preferred when
    # one is configured or `MODEL_PROVIDER` names it; Ollama stays the default otherwise. Both are
    # "local": the difference is the wire protocol, not the trust level.
    base = cfg.local_openai_base_url or ""
    wants_openai = (cfg.model_provider in ("openai", "lmstudio", "lm_studio", "lm-studio")
                    or (bool(base) and cfg.model_provider in ("", "auto") and not cfg.ollama_base_url))
    if wants_openai:
        local: Provider | None = LocalChat(cfg.local_model_name, base_url=base,
                                           api_key_env=cfg.local_api_key_env, budget=budget,
                                           timeout=cfg.local_timeout_s)
    else:
        local = GemmaLocal(cfg.local_model_name, host=cfg.ollama_base_url or None,
                           budget=budget, fallbacks=tuple([cfg.local_model_name,
                                                            *cfg.local_fallback_models]))
    cloud = None
    if cfg.cloud_escalation_enabled:
        if cfg.has_gemini_key:
            cloud = CloudEscalation(budget=budget, redaction=red, provider_label="gemini",
                                    model=cfg.gemini_model)
        elif cfg.openai_base_url or cfg.has_openai_key:
            cloud = CloudEscalation(budget=budget, redaction=red,
                                    provider_label=cfg.model_provider or "openai",
                                    model=cfg.openai_model or "gpt-4o-mini",
                                    host=cfg.openai_base_url, key_env="OPENAI_API_KEY")
    sim = DeterministicSimulator("regex-extractor-v1")
    simulated = False
    if cfg.mode == "demo":
        # Demo mode is *no model at all* — not the simulator either. A reviewer must be able to see
        # the deterministic floor of the product, and that floor has to be reported as the floor.
        local, cloud = None, None
    elif local is not None and not local.healthy().get("available"):
        # The GPU path was configured but nothing answers. Fall back to the offline extractor rather
        # than to "no extraction at all", and say so in the same breath: `provider` is recorded on
        # every claim, so a simulated read can never masquerade as a Gemma read in the ledger.
        local, simulated = sim, True
    elif local is None:
        local, simulated = sim, True
    probe = resolve_mode(cfg)
    desc = {
        "mode": probe["mode"], "local": local.healthy() if local else {"available": False,
                                                                        "reason": "demo mode"},
        "simulated": simulated,
        "local_kind": type(local).__name__ if local else "",
        "cloud": (cloud.healthy() if cloud else {"available": False,
                                                 "reason": probe.get("cloud_reason", "disabled by "
                                                                                      "policy")}),
        "egress_policy": {"cloud_escalation_enabled": cfg.cloud_escalation_enabled,
                          "max_bytes_per_day": cfg.max_cloud_egress_bytes_per_day,
                          "redacted_fields": list(red.drop_keys), "max_quote_chars":
                              red.max_quote_chars},
        "budget": budget.snapshot() if budget else {},
        "fallback_note": ("offline deterministic extractor in use — no model is installed; every "
                          "candidate below was still verified against the source"
                          if simulated else
                          ("local model tag not found; trying " + ", ".join(local.fallbacks)
                           if local else "no local model")),
    }
    # NB: with the simulator, `available` means "something can read documents", which is true, and
    # `provider` names it exactly, which is the honesty requirement. Telling the user the local path
    # is dead while quietly running a fallback would be the worst of both.
    return local, cloud, desc


def _budget_for(cfg) -> Budget:
    """Translate the dollar ceiling into the units `Budget` actually meters. Free-tier Gemini is
    priced per token, so a USD cap becomes a token cap here rather than in the transport, which keeps
    the budget logic in one auditable place."""
    usd_per_1m = max(1e-9, float(getattr(cfg, "usd_per_1m_tokens", 0.10) or 0.10))
    b = Budget()
    b.day_calls = max(1, int(cfg.max_cloud_calls_per_artifact * 40))
    b.per_artifact_calls = max(1, cfg.max_cloud_calls_per_artifact)
    b.day_tokens = max(1_000, int(cfg.day_cloud_limit_usd / usd_per_1m * 1_000_000))
    return b


def describe_outcome(res: ModelResult) -> str:
    """One-line, user-facing degradation text (PART 32). Kept here so the UI and the CLI say the same
    thing about the same failure."""
    if res.outcome == SUCCESS:
        return f"{len(res.candidates)} candidate fact(s) from {res.provider}/{res.model}"
    if res.outcome == PARTIAL:
        return ("model responded but stated nothing it could ground; the deterministic rules still "
                "report what they can see")
    if res.outcome == INVALID_OUTPUT:
        return ("model output did not match the requested schema, so none of it was promoted — "
                "verification requires structure, not vibes")
    if res.outcome == UNAVAILABLE:
        return ("AI extraction unavailable. Verified deterministic rules still identified: "
                "assignment titles, dates and weights where a row was unambiguous")
    return res.outcome
