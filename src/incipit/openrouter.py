"""OpenRouter chat-completions client for image-based transcription and metadata drafting."""

from __future__ import annotations

import base64
import json
import logging
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from . import fields, ledger
from .config import Settings

logger = logging.getLogger(__name__)

SPLIT_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "split",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "starts_new_document": {"type": "array", "items": {"type": "boolean"}},
                # A folder can accidentally include the same physical page scanned twice in a row
                # (double-feed, operator error) -- not a real second page.
                "is_duplicate_of_previous_page": {"type": "array", "items": {"type": "boolean"}},
            },
            "required": ["starts_new_document", "is_duplicate_of_previous_page"],
            "additionalProperties": False,
        },
    },
}


def page_counts(images: list[bytes]) -> dict:
    """What the document prompt needs to know about how many scans it is being given."""
    return {"page_count": len(images), "page_break_count": max(len(images) - 1, 0)}


def _image_block(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


# Every prompt comes from a compiled profile version (profiles.compiled), never from a file.


def build_split_messages(images: list[bytes], compiled) -> list[dict]:
    text = compiled.split_prompt.format(page_count=len(images))
    content = [{"type": "text", "text": text}] + [_image_block(img) for img in images]
    return [{"role": "user", "content": content}]


def extract_split_result(parsed: dict, usage: dict) -> dict:
    return {
        "starts_new_document": parsed.get("starts_new_document", []),
        "is_duplicate_of_previous_page": parsed.get("is_duplicate_of_previous_page", []),
        "usage": usage,
        "cost": usage.get("cost"),
    }


def build_new_document_messages(images: list[bytes], compiled) -> list[dict]:
    text = compiled.document_prompt.format(**page_counts(images))
    content = [{"type": "text", "text": text}] + [_image_block(img) for img in images]
    return [{"role": "user", "content": content}]


def document_extractor(registry: list[dict]):
    """The function that turns one draft's parsed JSON into {text, uncertain_words, fields}."""
    def extract(parsed: dict, usage: dict) -> dict:
        words = parsed.get("uncertain_words", [])
        return {
            "text": parsed.get("text", "") if isinstance(parsed.get("text"), str) else "",
            "uncertain_words": [w for w in words if isinstance(w, str)] if isinstance(words, list) else [],
            "fields": fields.extract(registry, parsed),
            "usage": usage,
            "cost": usage.get("cost"),
        }
    return extract


def document_chunk_merger(registry: list[dict]):
    """Joins the chunks of a document too large for one call (modeling.call_image_model)."""
    def merge(results: list[dict]) -> dict:
        return {
            "text": "\n\n".join(r["text"] for r in results if r.get("text")),
            "uncertain_words": fields.dedupe(
                [w for r in results for w in r.get("uncertain_words", [])]),
            "fields": fields.merge_chunks(registry, [r.get("fields", {}) for r in results]),
            "usage": {},
            "cost": sum(r.get("cost") or 0.0 for r in results),
        }
    return merge


def build_merge_metadata_messages(document_text: str, compiled) -> list[dict]:
    """A text-only request: the joined transcription in, document-level fields out."""
    text = compiled.merge_prompt.format(document_text=document_text or "[transcription unavailable]")
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def merge_extractor(registry: list[dict]):
    """A merge's answer: the document-scope fields only."""
    document_scope = [f for f in registry if f["scope"] == "document"]

    def extract(parsed: dict, usage: dict) -> dict:
        return {"fields": fields.extract(document_scope, parsed), "usage": usage, "cost": usage.get("cost")}
    return extract


def _session_with_retries(retries: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("POST",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _record_attempt(settings: Settings, payload: dict, record: dict) -> None:
    """One ledger row for one attempt: what it cost when the service said, an estimate when the
    answer was lost after sending, nothing when it was refused before any work."""
    usage = record["usage"] or {}
    cost, source = usage.get("cost"), "provider"
    if cost is None:
        cost, source = 0.0, "none"
    if record["outcome"] == "ambiguous":
        try:
            with ledger_connection(settings) as conn:
                cost = ledger.estimate(conn, ledger.current_context().get("purpose", "other"), payload.get("model", ""))
        except Exception:  # noqa: BLE001 - an estimate must never stop the call's own error
            cost = ledger.DEFAULT_ESTIMATE
        source = "estimated"
    fallback = getattr(settings, "openrouter_fallback_model", "")
    ledger.record(
        settings.db_path, started_at=record["started"], finished_at=ledger._now(),
        backend_kind="openrouter" if "openrouter.ai" in (settings.openrouter_base_url or "") else "openai_compatible",
        model=payload.get("model", ""), is_fallback=int(bool(fallback) and payload.get("model") == fallback),
        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        cost_usd=float(cost or 0.0), cost_source=source, outcome=record["outcome"],
        http_status=record["status"], error_text=record["error"])


def ledger_connection(settings: Settings):
    import sqlite3
    import contextlib

    @contextlib.contextmanager
    def opened():
        conn = sqlite3.connect(settings.db_path, timeout=5)
        try:
            yield conn
        finally:
            conn.close()
    return opened()


class ModelError(RuntimeError):
    """Base for every failure of an OpenRouter call."""


class TransientModelError(ModelError):
    """The request was fine; the service wasn't."""


class MalformedResponseError(TransientModelError):
    """Every attempt came back, but none of them as the JSON object the schema asks for."""


class AccountError(ModelError):
    """The account cannot make this call at all: no API key, an invalid one, or no credits."""


class RequestTooLargeError(ModelError):
    """This particular request exceeds the gateway's body-size limit."""


class ResponseTruncatedError(ModelError):
    """The reply was cut off at the output cap rather than finished."""


class ModelUnavailableError(ModelError):
    """No provider will serve this model right now."""


# Phrases OpenRouter uses when no provider will take the request, as opposed to one provider
# failing it.
NO_PROVIDER_PHRASES = (
    "exhausted all available targets",
    "no allowed providers",
    "no providers available",
    "not available",
    "no endpoints found",
)


class DegenerateResponseError(ModelError):
    """The model answered, at length, with almost no distinct words."""


# How much room the model is given to answer in.
MAX_OUTPUT_TOKENS = 16384
# Doubled up to, never asked for outright.
MAX_OUTPUT_TOKENS_CEILING = 32768
# Below this there is no point continuing to halve after a provider refuses the ask.
MIN_OUTPUT_TOKENS = 4096
# How a provider says the output budget was more than it allows.
MAX_TOKENS_REFUSED_PHRASES = (
    "max_tokens", "max tokens", "maximum tokens", "output token", "completion token",
)

# A degenerate response is long and repetitive, so both halves have to hold before we call it one.
DEGENERATE_MIN_CHARS = 400
DEGENERATE_MIN_WORDS = 50
DEGENERATE_UNIQUE_RATIO = 0.15
# Applied only on the retry after a degenerate answer.
DEGENERATE_RETRY_FREQUENCY_PENALTY = 0.5


def looks_degenerate(content: str) -> bool:
    """Whether a response has collapsed into a repetition loop."""
    if not content or len(content) < DEGENERATE_MIN_CHARS:
        return False
    words = content.split()
    if len(words) < DEGENERATE_MIN_WORDS:
        return False
    return len(set(words)) / len(words) < DEGENERATE_UNIQUE_RATIO


# Left between a fitted output budget and the context window, for the server's own count of the prompt.
CONTEXT_FIT_MARGIN = 256


def fitted_budget(detail: str) -> int | None:
    """The output budget that fits a refusal naming its context window and the prompt's tokens."""
    window = re.search(r"maximum context length is (\d+)", detail)
    prompt = re.search(r"(\d+) input tokens", detail)
    if not window or not prompt:
        return None
    return int(window.group(1)) - int(prompt.group(1)) - CONTEXT_FIT_MARGIN


def output_ceiling(messages: list[dict]) -> int:
    """The most this request may grow its answer to."""
    return MAX_OUTPUT_TOKENS_CEILING


# Below this, a body that stops mid-JSON is the provider giving up rather than a reply that ran out
# of room -- a different fault, whose remedy is another attempt and not a bigger budget.
TRUNCATION_MIN_CHARS = 2000

# Extra goes for a provider that abandons its own answer.
PROVIDER_ABORT_EXTRA_ATTEMPTS = 4


def looks_truncated(content: str) -> bool:
    """Whether a reply stopped in the middle of its JSON."""
    stripped = (content or "").strip()
    if len(stripped) < TRUNCATION_MIN_CHARS:
        return False
    # It has to have started as JSON to have been cut off in the middle of it.
    opener = stripped[0]
    if opener not in "{[":
        return False
    return not stripped.endswith("}" if opener == "{" else "]")


FENCE = "`" * 3


def _unfenced(content: str) -> str:
    """The reply's JSON, out of the Markdown code fence a model may wrap it in.

    A call that asks for a schema gets raw JSON back; one that does not is answered in prose
    habits, and a fence is the commonest. Without this the body parses as nothing at all, and the opening
    burns every attempt on both models before being given up on.
    """
    text = (content or "").strip()
    if not text.startswith(FENCE):
        return text
    body = text[len(FENCE):]
    newline = body.find("\n")
    # Whatever the fence was labelled with (json, JSON, nothing) sits on the opening line.
    if newline >= 0 and body[:newline].strip().isalpha():
        body = body[newline + 1:]
    closing = body.rfind(FENCE)
    return (body[:closing] if closing >= 0 else body).strip()


def _attribution_headers(settings: Settings) -> dict:
    """HTTP-Referer and X-Title, only when the site has set them."""
    headers = {}
    if settings.http_referer:
        headers["HTTP-Referer"] = settings.http_referer
    if settings.http_title:
        headers["X-Title"] = settings.http_title
    return headers


def call_openrouter(
    messages: list[dict],
    settings: Settings,
    schema: dict,
    extract_result,
    extra: dict | None = None,
) -> dict:
    """POST a chat completion request against the structured-output `schema`; `extract_result(parsed,
    usage)` shapes the parsed JSON body into this call's return dict."""
    # OpenRouter always needs a key; a server on the client's own network may need none.
    if not settings.openrouter_api_key and "openrouter.ai" in (settings.openrouter_base_url or ""):
        raise AccountError("no OpenRouter API key is set; enter one in Settings → Models")

    payload = {
        "model": settings.openrouter_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "reasoning": {"enabled": False},
        "response_format": schema,
        "usage": {"include": True},
        # Request settings only some callers send, such as a self-hosted server's repetition penalty.
        **(extra or {}),
    }
    if schema is not None:
        # Some models have a mix of providers, not all of which implement response_format.
        # OpenRouter documents require_parameters as the way to route a schema-backed request only
        # to endpoints that can honor every parameter instead of silently picking an incompatible
        # endpoint and hoping it fails cleanly.
        payload["provider"] = {"require_parameters": True}
    else:
        payload.pop("response_format")

    ceiling = output_ceiling(messages)
    session = _session_with_retries(settings.openrouter_retries)

    # `_session_with_retries` only retries transport-level failures (connection errors, 5xx/429
    # status codes).
    attempts = max(settings.openrouter_retries, 1)
    aborts = 0
    last_error: Exception | None = None
    no_provider = False
    # A while loop rather than `for ...
    attempt = 0
    while attempt < attempts:
        attempt += 1
        # Filled in as the attempt is understood, and written to the ledger however it ends.
        record = {"outcome": "error", "usage": {}, "status": None, "error": "", "started": ledger._now()}
        try:
            try:
                response = session.post(
                    f"{settings.openrouter_base_url}/chat/completions",
                    headers={
                        **({"Authorization": f"Bearer {settings.openrouter_api_key}"}
                           if settings.openrouter_api_key else {}),
                        "Content-Type": "application/json",
                        **_attribution_headers(settings),
                    },
                    json=payload,
                    timeout=settings.openrouter_timeout,
                )
            except requests.exceptions.Timeout as exc:
                record.update(outcome="ambiguous", error=f"no answer within {settings.openrouter_timeout:.0f}s")
                raise TransientModelError(f"{settings.openrouter_model} did not answer in time") from exc
            except requests.exceptions.RequestException as exc:
                record.update(outcome="error", error=str(exc)[:300])
                raise
            record["status"] = getattr(response, "status_code", None)
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                record.update(outcome="refused" if response.status_code < 500 else "error",
                              error=(getattr(response, "text", "") or "")[:300])
                # These are all problems retrying the identical request won't fix -- give a message a
                # reviewer can actually act on instead of "402 Client Error: Payment Required for url:
                # ...".
                reasons = {
                    401: ("invalid OpenRouter API key", "check the account's API key at https://openrouter.ai/settings/keys."),
                    402: ("insufficient OpenRouter credits", "check the account's credit balance at https://openrouter.ai/settings/credits."),
                    413: (
                        "request too large for OpenRouter's gateway (an image or window of images was too big)",
                        "this is a fixed request-size limit, not something retrying will fix.",
                    ),
                }
                # Some endpoints refuse to have reasoning switched off ("Reasoning is mandatory for
                # this endpoint and cannot be disabled").
                if response.status_code == 400 and "reasoning" in payload:
                    detail = ""
                    try:
                        detail = (response.json().get("error") or {}).get("message", "")
                    except ValueError:
                        pass
                    # A provider that will not allow the asked-for output budget.
                    lowered = detail.lower()
                    if any(phrase in lowered for phrase in MAX_TOKENS_REFUSED_PHRASES):
                        room = payload.get("max_tokens", MAX_OUTPUT_TOKENS)
                        fitted = fitted_budget(detail)
                        if room > MIN_OUTPUT_TOKENS:
                            # Fitting to the window keeps a dense page's room above 16,384 on a self-hosted server, where halving lost it.
                            payload["max_tokens"] = (fitted if fitted is not None and MIN_OUTPUT_TOKENS <= fitted < room
                                                     else max(room // 2, MIN_OUTPUT_TOKENS))
                            # A refused budget is the most this endpoint takes, so a cut-off reply may not double back to it.
                            ceiling = min(ceiling, payload["max_tokens"])
                            logger.warning(
                                "%s refused %d output tokens (%s); retrying with %d",
                                settings.openrouter_model, room, detail.strip() or "no message",
                                payload["max_tokens"],
                            )
                            attempts += 1
                            continue
                    if "reasoning is mandatory" in lowered:
                        logger.info(
                            "%s requires reasoning; resending without the reasoning keys",
                            settings.openrouter_model,
                        )
                        payload.pop("reasoning", None)
                        continue
                # A model id no provider serves.
                if response.status_code == 404:
                    detail = ""
                    try:
                        detail = (response.json().get("error") or {}).get("message", "")
                    except ValueError:
                        pass
                    raise ModelUnavailableError(
                        f"OpenRouter has no provider for {settings.openrouter_model} "
                        f"(404){': ' + detail if detail else ''}"
                    )
                if response.status_code in reasons:
                    reason, advice = reasons[response.status_code]
                    detail = ""
                    try:
                        detail = (response.json().get("error") or {}).get("message", "")
                    except ValueError:
                        pass
                    # 401/402 condemn the whole run (every later call uses the same key and balance);
                    # 413 condemns only this request, which may be one oversized document in a queue of
                    # ordinary ones.
                    error_class = RequestTooLargeError if response.status_code == 413 else AccountError
                    raise error_class(
                        f"OpenRouter rejected the request ({response.status_code}, {reason})"
                        f"{': ' + detail if detail else ''} -- {advice}"
                    ) from exc
                # An unclassified status.
                raise TransientModelError(
                    f"OpenRouter returned {response.status_code}: {response.text[:300]}"
                ) from exc

            content = ""
            try:
                try:
                    result = response.json()
                except ValueError:
                    # A free-tier model that has to queue gets held open with keep-alive padding
                    # around the body, and the parser rejects the whole reply over it. Observed live
                    # on qwen3.8-27b:free, which lost roughly one notebook opening in three. Retrying
                    # from the opening brace also covers padding that arrives mid-body.
                    body = getattr(response, "text", "") or ""
                    opener = body.find("{")
                    if opener < 0:
                        raise
                    result = json.loads(body[opener:])
                record["usage"] = result.get("usage") or {}

                # OpenRouter can return HTTP 200 with a body-level error object instead of choices --
                # observed live as {"error": {"message": "exhausted all available targets to no avail",
                # "code": 502}} when its upstream providers for the model were momentarily unavailable.
                body_error = result.get("error")
                if body_error:
                    code = body_error.get("code")
                    message = body_error.get("message", "")
                    record.update(outcome="error", error=message[:300])
                    # A size problem sometimes arrives here as a body error rather than a clean HTTP
                    # 413.
                    if code == 413 or "too large" in message.lower() or "exceeds" in message.lower():
                        raise RequestTooLargeError(
                            f"OpenRouter rejected the request (413, request too large): {message} -- this "
                            "is a fixed request-size limit, not something retrying will fix."
                        )
                    # "No provider will serve this model" is not the same as "a provider failed this
                    # request", though OpenRouter reports both as 502.
                    if any(phrase in message.lower() for phrase in NO_PROVIDER_PHRASES):
                        no_provider = True
                    # Retried like any transient upstream failure, same as a malformed-response glitch
                    # below.
                    raise ValueError(f"OpenRouter upstream error (code={code}): {message or 'no message'}")

                choice = (result.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content")
                if not content:
                    finish_reason = choice.get("finish_reason")
                    raise ValueError(f"empty response from {settings.openrouter_model} (finish_reason={finish_reason})")

                # Checked before parsing, because a repetition loop is often valid JSON -- the model
                # fills one string field with the same token until max_tokens and closes the object
                # correctly.
                if looks_degenerate(content):
                    record.update(outcome="malformed", error="a degenerate, repetitive reply")
                    words = content.split()
                    logger.warning(
                        "degenerate response from %s on attempt %d/%d (%d chars, %.3f unique-word "
                        "ratio, finish_reason=%s)",
                        settings.openrouter_model, attempt, attempts, len(content),
                        len(set(words)) / len(words), choice.get("finish_reason"),
                    )
                    if attempt < attempts:
                        payload["frequency_penalty"] = DEGENERATE_RETRY_FREQUENCY_PENALTY
                        continue
                    raise DegenerateResponseError(
                        f"{settings.openrouter_model} returned a degenerate response "
                        f"({len(content)} chars, {len(set(words)) / len(words):.3f} unique-word ratio) "
                        f"on every one of {attempts} attempt(s), including with a frequency penalty."
                    )

                # Checked before parsing, and checked after the degenerate test above: a reply stopped
                # at the cap is not malformed, it is unfinished, and the remedy is the opposite of a
                # retry.
                if choice.get("finish_reason") == "length":
                    record.update(outcome="malformed", error="the reply was cut off at the output cap")
                    room = payload.get("max_tokens", MAX_OUTPUT_TOKENS)
                    if room < ceiling:
                        payload["max_tokens"] = min(room * 2, ceiling)
                        logger.warning(
                            "%s hit the %d-token output cap on attempt %d/%d; retrying with %d",
                            settings.openrouter_model, room, attempt, attempts,
                            payload["max_tokens"],
                        )
                        # Deliberately outside the attempt budget.
                        attempts += 1
                        continue
                    raise ResponseTruncatedError(
                        f"{settings.openrouter_model} ran out of room at {room} output tokens: the "
                        "reply was cut off mid-transcription. The document is too long to transcribe "
                        "in one reply and needs to be sent in smaller pieces."
                    )

                parsed = json.loads(_unfenced(content))
                # Structured output asks for an object and does not always get one.
                if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                    logger.info(
                        "%s wrapped its result in a single-element array; unwrapping",
                        settings.openrouter_model,
                    )
                    parsed = parsed[0]
                # Every extractor calls .get on this.
                if not isinstance(parsed, dict):
                    raise ValueError(
                        f"{settings.openrouter_model} returned a JSON {type(parsed).__name__} where the "
                        "schema requires an object"
                    )
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if record["outcome"] != "error" or not record["error"]:
                    record.update(outcome="malformed", error=str(exc)[:300])
                # A body that stopped mid-JSON is unfinished, not malformed, and the remedy is more
                # room rather than another identical attempt.
                finish_reason = (result.get("choices") or [{}])[0].get("finish_reason")
                # The provider says it failed.
                if finish_reason == "error":
                    if aborts < PROVIDER_ABORT_EXTRA_ATTEMPTS:
                        aborts += 1
                        attempts += 1
                    logger.warning(
                        "%s aborted its own answer after %d character(s) (finish_reason=error) on "
                        "attempt %d/%d; retrying", settings.openrouter_model, len(content), attempt,
                        attempts,
                    )
                    continue
                if isinstance(exc, json.JSONDecodeError) and looks_truncated(content):
                    room = payload.get("max_tokens", MAX_OUTPUT_TOKENS)
                    if room < ceiling:
                        payload["max_tokens"] = min(room * 2, ceiling)
                        logger.warning(
                            "%s returned %d characters of unfinished JSON without saying so "
                            "(finish_reason=%s); retrying with %d output tokens",
                            settings.openrouter_model, len(content), finish_reason,
                            payload["max_tokens"],
                        )
                        attempts += 1          # deliberately outside the attempt budget
                        continue
                    raise ResponseTruncatedError(
                        f"{settings.openrouter_model} ran out of room at {room} output tokens: the "
                        "reply was cut off mid-transcription. The document is too long to transcribe "
                        "in one reply and needs to be sent in smaller pieces."
                    ) from exc
                # Truncate the raw body -- a degenerate response can be kilobytes of whitespace before
                # the actual error, which floods the terminal and buries the useful line.
                raw = response.text if len(response.text) <= 500 else response.text[:500] + "... [truncated]"
                logger.warning(
                    "openrouter call attempt %d/%d: %s; raw response (truncated): %r",
                    attempt, attempts, exc, raw,
                )
                continue

            usage = result.get("usage", {})
            record["outcome"] = "ok"
            return extract_result(parsed, usage)
        finally:
            _record_attempt(settings, payload, record)

    # Every attempt hit a malformed body or a transient upstream error.
    if no_provider:
        raise ModelUnavailableError(
            f"no provider served {settings.openrouter_model} across {attempts} attempt(s): {last_error}"
        ) from last_error
    # Malformed every time is this model's answer to this document, not an outage: say so, so the
    # caller hands the document to the fallback instead of asking the same model again.
    failure = (MalformedResponseError if isinstance(last_error, (json.JSONDecodeError, ValueError))
               else TransientModelError)
    raise failure(
        f"{settings.openrouter_model} did not return a usable response after {attempts} attempt(s): {last_error}"
    ) from last_error
