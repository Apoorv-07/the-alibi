"""alibi.config — one place where the outside world is allowed to touch the core.

Every knob is an environment variable, because the same code must run as: a laptop demo with a local
model, a hybrid build that escalates to a cloud API, and a container with no model at all. The mode is
*detected* rather than trusted: `resolve()` reports what the machine can actually do, and the UI shows
that — not what someone set in a .env and forgot.

No secret is ever read into a response object, logged, or handed to a template. `redacted_view()` is
what the /models endpoint is allowed to return.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Config:
    # --- deployment mode (PART 47) ---
    mode: str = "auto"                    # auto | demo | local | hybrid
    # --- paths ---
    database_url: str = ""                # "" -> <root>/alibi.db
    upload_dir: str = ""
    max_doc_bytes: int = 12 * 1024 * 1024
    # --- model providers (PART 5, 19, 48) ---
    model_provider: str = ""              # "" -> detect
    local_model_name: str = "gemma4:e4b"
    local_model_source: str = ""                # which env var supplied it ("" = built-in default)
    local_fallback_models: tuple[str, ...] = ("qwen3.5:4b", "llama3.2:3b", "gemma3:4b")
    ollama_base_url: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    openai_base_url: str = ""
    openai_model: str = ""
    # --- local OpenAI-compatible runtime (LM Studio is the intended one) ---
    # `local_openai_base_url` is the *loopback* path: un-redacted payload, egress=LOCAL. The cloud
    # `openai_base_url` is the escalation path: redacted, budgeted. Which one a URL becomes is decided
    # by `providers.is_loopback`, never by the name of the variable.
    local_openai_base_url: str = ""
    local_api_key_env: str = "LM_STUDIO_API_KEY"
    local_timeout_s: float = 90.0
    max_output_tokens: int = 2048         # model output is untrusted *and* bounded (PART 33)
    # --- escalation + budget (PART 18, 19) ---
    cloud_escalation_enabled: bool = True
    max_cloud_egress_bytes_per_day: int = 4_000_000
    day_cloud_limit_usd: float = 0.50
    max_cloud_calls_per_artifact: int = 4
    confidence_floor: float = 0.72
    # --- trust (PART 11) ---
    supersede_trust_floor: float = 0.5
    attendance_threshold: float = 0.75
    condonation_floor: float = 0.65
    institution_config: str = ""          # path to config/institutions/<x>.yaml
    # --- action safety (PART 16) ---
    require_action_approval: bool = True
    dry_run: bool = True                  # no real external writes unless explicitly turned off
    allow_irreversible: bool = False      # 'submit assignment' / 'drop course' never auto-execute
    # --- observability (PART 34) ---
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    # --- retention (PART 18) ---
    retention_days: int = 365
    raw_content_retention_days: int = 30  # keep receipts, drop raw text sooner by default

    secrets: dict[str, bool] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- setup ---
    @classmethod
    def from_env(cls) -> "Config":
        c = cls(
            mode=(os.environ.get("ALIBI_MODE") or "auto").strip().lower(),
            # ALIBI_DB is what the CLI and the server use to pick the file; if Config did not read it, the
            # /privacy page, the budget table and the wipe verification would be reporting on a *different*
            # database than the one holding your ledger (measured inside the container, where ALIBI_DB is the
            # only path set and the default landed on /app/alibi.db).
            database_url=(os.environ.get("DATABASE_URL") or os.environ.get("ALIBI_DB") or ""),
            upload_dir=os.environ.get("ALIBI_UPLOAD_DIR", ""),
            max_doc_bytes=int(_num("ALIBI_MAX_DOC_BYTES", 12 * 1024 * 1024)),
            model_provider=(os.environ.get("MODEL_PROVIDER") or "").strip().lower(),
            # LM_STUDIO_MODEL is an alias for LOCAL_MODEL_NAME, not a second setting: one name, two
            # spellings, and `local_model_source` records which one actually spoke so /settings can say
            # "model came from LM_STUDIO_MODEL" instead of silently preferring an unrelated variable.
            local_model_name=(os.environ.get("LM_STUDIO_MODEL") or os.environ.get("LOCAL_MODEL_NAME")
                              or "gemma4:e4b"),
            ollama_base_url=os.environ.get("OLLAMA_BASE_URL", ""),
            gemini_model=os.environ.get("ALIBI_GEMINI_MODEL", "gemini-2.5-flash-lite"),
            # Every name a user is likely to type is accepted, and exactly one wins in the documented
            # order: LM_STUDIO_* (this product's supported local runtime), then the generic OpenAI-compat
            # variables, then nothing. Recording *which* one won is what makes /settings honest.
            openai_base_url=os.environ.get("OPENAI_BASE_URL", ""),
            openai_model=os.environ.get("OPENAI_MODEL", ""),
            local_openai_base_url=(os.environ.get("LM_STUDIO_BASE_URL")
                                   or os.environ.get("LOCAL_OPENAI_BASE_URL")
                                   or os.environ.get("OPENAI_BASE_URL", "")),
            local_api_key_env=(os.environ.get("LM_STUDIO_API_KEY_ENV") or "LM_STUDIO_API_KEY"),
            local_timeout_s=_num("LM_STUDIO_TIMEOUT_S", 90.0),
            max_output_tokens=int(_num("ALIBI_MAX_OUTPUT_TOKENS", 2048)),
            cloud_escalation_enabled=_flag("CLOUD_ESCALATION_ENABLED", True),
            max_cloud_egress_bytes_per_day=int(_num("MAX_CLOUD_EGRESS", 4_000_000)),
            day_cloud_limit_usd=_num("ALIBI_DAY_LIMIT_USD", 0.50),
            max_cloud_calls_per_artifact=int(_num("ALIBI_MAX_CLOUD_CALLS", 4)),
            confidence_floor=_num("ALIBI_CONFIDENCE_FLOOR", 0.72),
            supersede_trust_floor=_num("ALIBI_SUPERSEDE_TRUST_FLOOR", 0.5),
            attendance_threshold=_num("ALIBI_ATTENDANCE_THRESHOLD", 0.75),
            condonation_floor=_num("ALIBI_CONDONATION_FLOOR", 0.65),
            institution_config=os.environ.get("ALIBI_INSTITUTION_CONFIG", ""),
            require_action_approval=_flag("REQUIRE_ACTION_APPROVAL", True),
            dry_run=_flag("DRY_RUN", True),
            allow_irreversible=_flag("ALIBI_ALLOW_IRREVERSIBLE", False),
            debug=_flag("DEBUG", False),
            log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
            log_json=_flag("ALIBI_LOG_JSON", True),
            retention_days=int(_num("RETENTION_DAYS", 365)),
            raw_content_retention_days=int(_num("ALIBI_RAW_RETENTION_DAYS", 30)),
            local_model_source=("LM_STUDIO_MODEL" if os.environ.get("LM_STUDIO_MODEL")
                                else ("LOCAL_MODEL_NAME" if os.environ.get("LOCAL_MODEL_NAME") else "")),
            secrets={k: bool(os.environ.get(k)) for k in _SECRET_ENVS},
        )
        if c.mode not in {"auto", "demo", "local", "hybrid"}:
            c.mode = "auto"
        return c

    # ------------------------------------------------------------------ io ---
    @property
    def db_path(self) -> Path:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.replace("sqlite:///", ""))
        if self.database_url:
            return Path(self.database_url)
        return ROOT / "alibi.db"

    @property
    def uploads(self) -> Path:
        return Path(self.upload_dir) if self.upload_dir else ROOT / "var" / "uploads"

    @property
    def has_gemini_key(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_GEMINI_API_KEY"))

    @property
    def has_openai_key(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY") or self.openai_base_url)

    def redacted_view(self) -> dict:
        """Safe for the UI. Presence booleans only — a key is never returned, echoed or logged."""
        return {
            "mode": self.mode,
            "database": str(self.db_path.name),
            "local_model": self.local_model_name,
            "ollama_base_url": self.ollama_base_url or "http://127.0.0.1:11434",
            "local_openai_base_url": self.local_openai_base_url or "",
            "local_is_loopback": is_loopback_hint(self.local_openai_base_url),
            "local_model_source": self.local_model_source or "(built-in default)",
            "local_api_key_present": bool(os.environ.get(self.local_api_key_env)),
            "cloud_escalation_enabled": self.cloud_escalation_enabled,
            "budget_usd_per_day": self.day_cloud_limit_usd,
            "max_cloud_egress_bytes_per_day": self.max_cloud_egress_bytes_per_day,
            "max_cloud_calls_per_artifact": self.max_cloud_calls_per_artifact,
            "confidence_floor": self.confidence_floor,
            "supersede_trust_floor": self.supersede_trust_floor,
            "require_action_approval": self.require_action_approval,
            "dry_run": self.dry_run,
            "allow_irreversible": self.allow_irreversible,
            "retention_days": self.retention_days,
            "raw_content_retention_days": self.raw_content_retention_days,
            "secrets_present": dict(self.secrets),
        }


_SECRET_ENVS = ("LM_STUDIO_API_KEY", "GEMINI_API_KEY", "GOOGLE_GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
                "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN")


def is_loopback_hint(url: str) -> str:
    """"local" | "remote" | "unset" for the settings page. Delegates to the same predicate the egress
    decision uses, so the UI cannot advertise a privacy property the transport does not honour."""
    if not url:
        return "unset"
    from .providers import _host_of, is_loopback
    return "local" if is_loopback(_host_of(url)) else "remote"


def load_institution_policy(path: str | None) -> dict:
    """Institution-specific trust and grading policy (PART 11, USP-11). YAML if available, else a
    built-in default. Config, not code, is what makes this portable across colleges."""
    default = {
        "name": "tn-default",
        "attendance_threshold": 0.75,
        "condonation_floor": 0.65,
        "tie_break": "earliest_safe",
        "high_weight": 0.10,
        "supersede_trust_floor": 0.5,
        "precedence": ["lms_api", "email_thread", "notice_photo", "chat_export", "portal_pdf",
                       "syllabus_pdf"],
        "authority": {"lms_api": 0.9, "email_thread": 0.8, "professor_announcement": 0.75,
                      "notice_photo": 0.6, "syllabus_pdf": 0.6, "calendar_event": 0.55,
                      "manual_entry": 0.5, "erp_table": 0.5, "student_message": 0.35,
                      "class_group": 0.4, "rumour": 0.2, "ocr_image": 0.45},
        "late_policy_default": {"pct_per_day": 0.0, "max_days": 0},
        "tz": "Asia/Kolkata",
        "term_anchor": "2026-09-22",
        "defaulter_freeze": "2026-10-30",
        "rationale": "AICTE/UGC 75% per subject incl. lab; condonation to 65% only with a "
                     "certificate filed before the freeze date.",
    }
    if not path:
        return default
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return default
    text = p.read_text()
    try:
        import yaml
        loaded = yaml.safe_load(text) or {}
    except Exception:
        loaded = {}
        for line in text.splitlines():          # 12-hour-friendly fallback for flat yaml
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                loaded[k.strip()] = v.strip().strip("'\"")
    out = {**default, **{k: v for k, v in loaded.items() if v not in (None, "")}}
    out["_source_file"] = str(p)
    return out


def resolve_mode(cfg: Config) -> dict:
    """What this machine can *actually* do, probed not assumed (PART 5, 32, 47)."""
    probe = {"local_ok": False, "local_model": "", "cloud_ok": False, "cloud_reason": "",
             "mode": "rules_only"}
    if cfg.mode == "demo":
        probe["mode"] = "demo"
        return probe
    # A local OpenAI-compatible runtime (LM Studio) is probed first when configured, because that is
    # the documented path; Ollama is the fallback probe. Both answer the same question: can this machine
    # read a document with a model, right now, without sending bytes anywhere else?
    if cfg.model_provider in ("openai", "lmstudio", "lm-studio", "lm_studio") or cfg.local_openai_base_url:
        try:
            from .providers import LocalChat
            lc = LocalChat(cfg.local_model_name, base_url=cfg.local_openai_base_url,
                           api_key_env=cfg.local_api_key_env, timeout=cfg.local_timeout_s)
            h = lc.healthy()
            if h.get("available"):
                probe["local_ok"] = True
                probe["local_model"] = h.get("model") or cfg.local_model_name
                probe["local_provider"] = "lm-studio" if h.get("loopback") else "openai-compat"
            else:
                probe["local_reason"] = h.get("reason", "")[:160]
                probe["local_hint"] = h.get("hint", "")
                probe["local_models"] = h.get("installed", [])
        except Exception as e:
            probe["local_reason"] = f"{type(e).__name__}: {e}"[:160]
    if not probe["local_ok"] and cfg.model_provider in ("", "ollama", "auto"):
        try:
            from .adapters import Ollama
            o = Ollama(model=cfg.local_model_name, host=cfg.ollama_base_url or None)
            if o.healthy():
                probe["local_ok"] = True
                probe["local_provider"] = "ollama"
                probe["local_model"] = getattr(o, "resolved_model", cfg.local_model_name) or \
                    cfg.local_model_name
        except Exception as e:
            probe["local_reason"] = f"{type(e).__name__}"
    if cfg.cloud_escalation_enabled and (cfg.has_gemini_key or cfg.has_openai_key):
        probe["cloud_ok"] = True
    elif cfg.cloud_escalation_enabled:
        probe["cloud_reason"] = "no API key in environment"
    if probe["local_ok"] and probe["cloud_ok"]:
        probe["mode"] = "hybrid"
    elif probe["local_ok"]:
        probe["mode"] = "local"
    elif probe["cloud_ok"]:
        probe["mode"] = "cloud_only"
    else:
        probe["mode"] = "rules_only"
    return probe
