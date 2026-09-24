"""FastAPI application: adaptive quiz item selection engine.

Flow
----
1. ``POST /api/sessions``            create an examinee session
2. ``POST /api/sessions/{id}/next``  get the most informative eligible item
3. ``POST /api/sessions/{id}/responses``  submit an answer; theta is updated
4. repeat 2-3 until the session finishes (max items, no eligible item,
   expired deadline, or -- when configured -- the precision stop rule
   ``finish_reason = 'precision_reached'``)

An active, unexpired session may be paused
(``POST /api/sessions/{id}/pause``): the remaining time is frozen and /next
plus new answers are rejected with 409.  Resuming
(``POST /api/sessions/{id}/resume``) recomputes the UTC deadline from the
frozen seconds and restores the pending item, stored answers and counted
exposures; time-limit-free sessions can be paused and resumed as well.

``POST /api/items/import`` bulk-loads the item bank with per-row
validation: a dry run only reports the planned created/updated rows, while
a real commit upserts every row in one serialised transaction (updates
keep ``exposure_count`` and ``is_active``) and leaves the bank untouched
if any row fails.

``PATCH /api/items/{id}/availability`` withdraws or re-enables one item.
A withdrawn item no longer takes part in new selections; a session whose
pending item was withdrawn clears it and re-selects inside the /next
transaction (the exposure already counted is not rolled back), and new
answers for it are rejected with 409 while a stored ``response_id``
replay still returns the first result.

Mutating endpoints use ``BEGIN IMMEDIATE`` so concurrent requests on the same
SQLite database are serialised; exposure counts and ability updates therefore
cannot be lost to read/write races.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import irt
from .analysis import analyze_item
from .database import get_db, init_db
from .importer import NormalizedItem, RowError, validate_rows
from .models import (
    BankItemOut,
    HealthOut,
    ItemAnalysisOut,
    ItemAvailabilityUpdate,
    ItemBankOut,
    ItemDetailOut,
    ItemImportErrorOut,
    ItemImportRequest,
    ItemImportResponse,
    ItemImportRowOut,
    ItemOut,
    NextItemOut,
    ProgressOut,
    ResponseCreate,
    ResponseHistoryOut,
    ResponseOut,
    SessionCreate,
    SessionOut,
)

DEFAULT_EXPOSURE_LIMIT = int(os.getenv("DEFAULT_EXPOSURE_LIMIT", "5"))

app = FastAPI(
    title="自适应测验选题 API",
    description=(
        "基于 2PL IRT 模型的计算机自适应测验（CAT）："
        "单次 Newton 更新估计能力，信息量最大选题，全局曝光上限控制；"
        "活动会话支持暂停（冻结剩余时限）与恢复（按剩余秒数重算 UTC 截止时间）；"
        "题目可撤回/启用，撤回题不再参与新取题。"
    ),
    version="1.4.0",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> Response:
    """422 handler whose body is always strict-JSON serialisable.

    FastAPI echoes the offending input in each error, and a non-finite
    float (``Infinity``/``NaN`` in a non-strict request body) makes the
    default handler itself crash with 500 while rendering that echo.
    Replace such values with a marker string instead.
    """

    def sanitize(value: object) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            return "<non-finite>"
        if isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(v) for v in value]
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        # Pydantic keeps the original exception object in a model-validator
        # error's ctx; render anything else as its string form.
        return str(value)

    body = json.dumps(
        {"detail": sanitize(exc.errors())}, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return Response(
        content=body, status_code=422, media_type="application/json"
    )


# -------------------------------------------------------------------------
# Lifecycle / small helpers
# -------------------------------------------------------------------------

@app.on_event("startup")
def _startup() -> None:
    init_db()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialised write transaction (BEGIN IMMEDIATE) with explicit rollback."""
    conn.isolation_level = None  # autocommit mode: we manage the transaction
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    """Parse a stored ISO-8601 UTC timestamp into an aware datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:  # defensive: timestamps are always written as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expire_if_due(
    conn: sqlite3.Connection,
    session: sqlite3.Row,
    now: datetime,
) -> sqlite3.Row | None:
    """Finish an active session whose deadline has passed.

    Must be called first thing inside a serialised write transaction.  When
    the session has an ``expires_at`` in the past it is marked finished with
    ``finish_reason = 'time_expired'`` and the pending item is cleared; the
    pending item does *not* get another exposure (selection already counted
    one) and no response row is written.

    The finishing UPDATE is committed immediately (still inside the same
    serialised transaction, before any other write) and the connection is
    returned to autocommit mode, so the caller can raise the 409 without the
    surrounding ``transaction()`` rolling the expiry back.  Returns the
    refreshed row when the session was expired, otherwise ``None``.
    """
    if session["status"] != "active" or session["expires_at"] is None:
        return None
    expires_at = parse_utc(session["expires_at"])
    if now < expires_at:
        return None
    conn.execute(
        """UPDATE sessions
           SET status = 'finished', finish_reason = 'time_expired',
               current_item_id = NULL, updated_at = ?
           WHERE id = ? AND status = 'active'""",
        (now.isoformat(timespec="seconds"), session["id"]),
    )
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session["id"],)
    ).fetchone()
    conn.commit()
    return row


def time_expired_409(expired: sqlite3.Row) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "测验时限已到（UTC 截止时间 "
            f"{expired['expires_at']}），会话已结束："
            "finish_reason=time_expired"
        ),
    )


def session_paused_409(session: sqlite3.Row) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"会话已暂停（paused_at={session['paused_at']}）："
            "暂停期间不能取题或作答，请先调用 "
            f"POST /api/sessions/{session['id']}/resume 恢复"
        ),
    )


def session_not_active_409(session: sqlite3.Row, *, verb: str) -> HTTPException:
    if verb == "恢复":
        requirement = "只有暂停中（paused）的会话可以恢复"
    else:
        requirement = "只有进行中（active）且未到期的会话可以暂停"
    return HTTPException(
        status_code=409,
        detail=f"{requirement}，当前 status={session['status']}",
    )


def session_remaining_seconds(
    session: sqlite3.Row, now: datetime
) -> int | None:
    """Seconds left on the time limit.

    * paused session -> the value frozen at pause time (the clock does not
      tick while paused);
    * active session with a deadline -> live ``expires_at - now`` countdown,
      floored at zero (an active session sitting on its deadline is treated
      as having no time left and gets closed by the next write as
      ``time_expired``);
    * no time limit / finished session -> ``None``.
    """
    if session["status"] == "paused":
        return session["remaining_seconds"]
    if session["status"] != "active" or session["expires_at"] is None:
        return None
    delta = (parse_utc(session["expires_at"]) - now).total_seconds()
    return max(0, int(delta))


def get_session_or_404(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


def parse_options(raw: str) -> list[dict]:
    return json.loads(raw)


def serialize_item(row: sqlite3.Row, *, reveal: bool = False) -> ItemOut:
    return ItemOut(
        id=row["id"],
        difficulty=row["difficulty"],
        discrimination=row["discrimination"],
        content_tag=row["content_tag"],
        stem=row["stem"],
        options=parse_options(row["options"]),
    )


def serialize_bank_item(row: sqlite3.Row) -> BankItemOut:
    return BankItemOut(
        id=row["id"],
        difficulty=row["difficulty"],
        discrimination=row["discrimination"],
        content_tag=row["content_tag"],
        stem=row["stem"],
        answer=row["answer"],
        exposure_count=row["exposure_count"],
        is_active=bool(row["is_active"]),
    )


def answered_count(conn: sqlite3.Connection, session_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM responses WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def session_standard_error(
    conn: sqlite3.Connection, session_id: str, theta: float
) -> float | None:
    """SE of the ability estimate: ``1 / sqrt(sum I_j(theta))``.

    Fisher information of *every* item already answered in this session is
    re-evaluated at the current theta (2PL).  Returns ``None`` when no item
    has been answered or the summed information is not positive; the
    precision stop rule can never fire in that case.
    """
    rows = conn.execute(
        """
        SELECT i.discrimination AS a, i.difficulty AS b
        FROM responses r JOIN items i ON i.id = r.item_id
        WHERE r.session_id = ?
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        return None
    total_info = math.fsum(irt.information(theta, r["a"], r["b"]) for r in rows)
    return irt.standard_error(total_info)


def tag_minimums_for(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Persisted content-tag quotas of a session (empty dict when none set)."""
    return {
        row["content_tag"]: row["minimum"]
        for row in conn.execute(
            "SELECT content_tag, minimum FROM session_tag_minimums "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    }


def answered_by_tag(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Number of already-answered items per non-empty content tag."""
    counts: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT i.content_tag AS content_tag, COUNT(*) AS n
        FROM responses r JOIN items i ON i.id = r.item_id
        WHERE r.session_id = ? AND i.content_tag IS NOT NULL
          AND i.content_tag <> ''
        GROUP BY i.content_tag
        """,
        (session_id,),
    ).fetchall()
    for row in rows:
        counts[row["content_tag"]] = row["n"]
    return dict(counts)


def tag_shortfalls(
    minimums: dict[str, int], answered: dict[str, int]
) -> dict[str, int]:
    """Remaining quota deficit per tag; tags already met are omitted."""
    return {
        tag: min_qty - answered.get(tag, 0)
        for tag, min_qty in minimums.items()
        if answered.get(tag, 0) < min_qty
    }


def serialize_session(
    row: sqlite3.Row,
    n_answered: int,
    tag_minimums: dict[str, int] | None = None,
    standard_error: float | None = None,
) -> SessionOut:
    return SessionOut(
        id=row["id"],
        theta=row["theta"],
        current_item_id=row["current_item_id"],
        max_items=row["max_items"],
        exposure_limit=row["exposure_limit"],
        duration_seconds=row["duration_seconds"],
        expires_at=row["expires_at"],
        paused=row["status"] == "paused",
        paused_at=row["paused_at"],
        remaining_seconds=session_remaining_seconds(
            row, datetime.now(timezone.utc)
        ),
        tag_minimums=tag_minimums or {},
        target_se=row["target_se"],
        min_precision_items=row["min_precision_items"],
        standard_error=standard_error,
        status=row["status"],
        finish_reason=row["finish_reason"],
        answered_count=n_answered,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def serialize_response(row: sqlite3.Row) -> ResponseOut:
    return ResponseOut(
        id=row["id"],
        session_id=row["session_id"],
        item_id=row["item_id"],
        response=row["response"],
        correct=bool(row["score"]),
        score=row["score"],
        correct_answer=row["correct_answer"],
        theta_before=row["theta_before"],
        theta_after=row["theta_after"],
        theta_change=row["theta_after"] - row["theta_before"],
        p_correct=row["p_correct"],
        information_at_theta=row["information_at_theta"],
        exposure_count=row["exposure_count_at_select"],
        created_at=row["created_at"],
    )


# -------------------------------------------------------------------------
# Health / docs
# -------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> HealthOut:
    bank_size = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return HealthOut(status="ok", bank_size=bank_size)


# -------------------------------------------------------------------------
# Item bank (read-only inspection)
# -------------------------------------------------------------------------

@app.get("/api/items", response_model=ItemBankOut, tags=["items"])
def list_items(
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
) -> ItemBankOut:
    rows = conn.execute(
        "SELECT * FROM items ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return ItemBankOut(count=len(rows), items=[serialize_bank_item(r) for r in rows])


@app.get("/api/items/{item_id}", response_model=ItemDetailOut, tags=["items"])
def get_item(
    item_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ItemDetailOut:
    row = conn.execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    base = serialize_bank_item(row).model_dump()
    base["options"] = parse_options(row["options"])
    return ItemDetailOut(**base)


# -------------------------------------------------------------------------
# Item availability (withdraw / re-enable)
# -------------------------------------------------------------------------

@app.patch(
    "/api/items/{item_id}/availability",
    response_model=BankItemOut,
    tags=["items"],
    summary="撤回 / 启用题目（撤回题不再参与新取题）",
)
def set_item_availability(
    item_id: str,
    body: ItemAvailabilityUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> BankItemOut:
    """Withdraw or re-enable one item; unknown ids get a 404.

    ``{"is_active": false}`` withdraws the item: it stops being eligible
    for new selections, while every stored response, counted exposure and
    session history is kept.  A session whose *pending* item was withdrawn
    clears the pending marker and re-selects by the normal rules inside
    the next ``/next`` transaction (the exposure already counted for the
    withdrawn item is **not** rolled back), and a *new* answer for the
    withdrawn item is rejected with 409 — a replayed ``response_id`` is
    still served from the idempotency store.  ``{"is_active": true}``
    re-enables the item.  The toggle itself is idempotent: setting the
    same state again simply returns the current row.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="item not found")
        conn.execute(
            "UPDATE items SET is_active = ? WHERE id = ?",
            (1 if body.is_active else 0, item_id),
        )
        row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    return serialize_bank_item(row)


@app.get(
    "/api/items/{item_id}/analysis",
    response_model=ItemAnalysisOut,
    tags=["items"],
)
def item_analysis(
    item_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ItemAnalysisOut:
    """Review one item against the answers saved by finished sessions.

    Strictly read-only: no session, exposure or bank row is touched.  Each
    finished session that answered the item *and* at least one other item
    contributes one sample of (target score, ratio of correct peer items);
    both numbers use the ``score`` frozen at answer time, so later bank
    edits (including a changed correct answer) cannot change the result.
    Returns ``pearson_correlation = null`` when fewer than 5 samples exist or
    either series has zero variance, and 404 when the item is unknown.
    """
    exists = conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="item not found")
    result = analyze_item(conn, item_id)
    return ItemAnalysisOut(
        item_id=item_id,
        sample_count=result.sample_count,
        correct_rate=result.correct_rate,
        pearson_correlation=result.pearson_r,
    )


# -------------------------------------------------------------------------
# Item bank bulk import / pre-check
# -------------------------------------------------------------------------

# SQLite caps the number of host parameters at 999 by default; keep every
# chunk of the existing-id lookup well below that limit.
_EXISTING_ID_CHUNK = 500

_UPSERT_ITEM_SQL = """
INSERT INTO items
    (id, difficulty, discrimination, content_tag, stem,
     options, answer, exposure_count)
VALUES (?, ?, ?, ?, ?, ?, ?, 0)
ON CONFLICT(id) DO UPDATE SET
    difficulty     = excluded.difficulty,
    discrimination = excluded.discrimination,
    content_tag    = excluded.content_tag,
    stem           = excluded.stem,
    options        = excluded.options,
    answer         = excluded.answer
    -- exposure_count is intentionally NOT updated: a re-import must never
    -- reset the global exposure accounting of an already-served item.
    -- is_active is intentionally NOT updated either: re-importing an
    -- existing item keeps its withdrawn/enabled state, while a brand-new
    -- row takes the column DEFAULT (1 = enabled).
"""


def _existing_item_ids(
    conn: sqlite3.Connection, item_ids: list[str]
) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(item_ids), _EXISTING_ID_CHUNK):
        chunk = item_ids[start : start + _EXISTING_ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM items WHERE id IN ({placeholders})", chunk
            ).fetchall()
        )
    return found


def _import_payload(
    *,
    dry_run: bool,
    total: int,
    errors: list[RowError],
    items: list[NormalizedItem] | None = None,
    existing_ids: set[str] | None = None,
) -> tuple[ItemImportResponse, int]:
    """Build the import response.

    Returns ``(payload, status_code)``.  When *errors* is non-empty no
    created/updated classification is returned (the batch cannot be applied
    as-is), and a real (non-dry-run) request is answered with 422.  With a
    clean batch every valid row is classified against *existing_ids*:
    ids already in the bank are ``updated``, the rest ``created``.
    """
    error_out = [
        ItemImportErrorOut(
            row=e.row, code=e.code, message=e.message, field=e.field, id=e.id
        )
        for e in errors
    ]
    valid = not errors
    existing_ids = existing_ids or set()
    results: list[ItemImportRowOut] = []
    created = updated = 0
    if valid and items is not None:
        # validate_rows already returns rows in request order; sort once more
        # defensively so the stable 1-based row numbering is guaranteed.
        for item in sorted(items, key=lambda n: n.row):
            if item.id in existing_ids:
                action = "updated"
                updated += 1
            else:
                action = "created"
                created += 1
            results.append(ItemImportRowOut(row=item.row, id=item.id, action=action))
    status_code = 200
    if not valid and not dry_run:
        status_code = 422
    return (
        ItemImportResponse(
            dry_run=dry_run,
            valid=valid,
            created_count=created,
            updated_count=updated,
            total=total,
            results=results,
            errors=error_out,
        ),
        status_code,
    )


@app.post(
    "/api/items/import",
    response_model=ItemImportResponse,
    tags=["items"],
    summary="题库批量导入 / 预检（逐行错误、事务原子 upsert、更新保留曝光数与启用状态）",
)
def import_items(
    body: ItemImportRequest, conn: sqlite3.Connection = Depends(get_db)
) -> ItemImportResponse | JSONResponse:
    """Validate and (optionally) bulk-insert/update item-bank rows.

    Every row is validated independently and *all* problems are reported at
    once against the row's stable 1-based index in the ``items`` array:

    * ``id`` — required, non-blank string, at most 128 chars; duplicate ids
      inside the same batch are rejected on every occurrence after the first
      (``duplicate_id_in_batch``);
    * ``options`` — 2~8 objects, each with a non-blank ``key`` and ``text``;
      keys are trimmed/upper-cased and must be unique within the row;
    * ``answer`` — must equal one of the (normalised) option keys;
    * ``difficulty`` — finite number in [-6, 6] (booleans/NaN/Infinity
      rejected);
    * ``discrimination`` — positive finite number in (0, 5];
    * ``content_tag`` — string or null; a blank/whitespace tag is
      normalised to null.

    ``dry_run=true`` is a pure pre-check: nothing is written; each valid row
    is still classified as ``created`` or ``updated`` according to the
    current bank and returned with HTTP 200, while any row errors are
    reported too.

    A real commit applies every row in **one** serialised
    (``BEGIN IMMEDIATE``) transaction via upsert: unknown ids are inserted
    and existing ids are updated in place.  An update overwrites
    difficulty/discrimination/content_tag/stem/options/answer but
    **preserves ``exposure_count`` and ``is_active``**, so re-importing a
    bank never resets global exposure accounting and never re-enables a
    withdrawn item (a brand-new row starts enabled).  If any row fails
    validation nothing is written at all and the response is HTTP 422
    (``valid=false``, counts zero); a database-level failure rolls the
    whole batch back.

    ``results`` and ``errors`` are both ordered by the stable row number, so
    callers can splice them back into their original spreadsheet lines.
    """
    total = len(body.items)
    items, errors = validate_rows(body.items)

    if errors:
        payload, status_code = _import_payload(
            dry_run=body.dry_run, total=total, errors=errors
        )
        return JSONResponse(
            status_code=status_code, content=payload.model_dump()
        )

    if body.dry_run:
        # Read-only snapshot; no write lock is taken and nothing is written.
        existing_ids = _existing_item_ids(conn, [i.id for i in items])
        payload, _ = _import_payload(
            dry_run=True,
            total=total,
            errors=[],
            items=items,
            existing_ids=existing_ids,
        )
        return payload

    with transaction(conn):
        # Re-classify inside the write lock: the serialised transaction makes
        # the exists-check plus upsert atomic against concurrent imports.
        existing_ids = _existing_item_ids(conn, [i.id for i in items])
        conn.executemany(
            _UPSERT_ITEM_SQL,
            [
                (
                    item.id,
                    item.difficulty,
                    item.discrimination,
                    item.content_tag,
                    item.stem,
                    json.dumps(item.options, ensure_ascii=False),
                    item.answer,
                )
                for item in items
            ],
        )
    payload, _ = _import_payload(
        dry_run=False,
        total=total,
        errors=[],
        items=items,
        existing_ids=existing_ids,
    )
    return payload


# -------------------------------------------------------------------------
# Sessions
# -------------------------------------------------------------------------

@app.post(
    "/api/sessions",
    response_model=SessionOut,
    status_code=201,
    tags=["sessions"],
)
def create_session(
    body: SessionCreate, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    session_id = uuid.uuid4().hex
    created = datetime.now(timezone.utc)
    now = created.isoformat(timespec="seconds")
    duration = body.duration_seconds
    expires_at = (
        (created + timedelta(seconds=duration)).isoformat(timespec="seconds")
        if duration is not None
        else None
    )
    with transaction(conn):
        minimums = body.tag_minimums or {}
        if minimums:
            # Every quota key must reference a non-empty content tag that
            # actually exists in the bank; the quota total must leave room
            # inside max_items (positive ints are already enforced by the
            # request model).  422 mirrors Pydantic-level request errors.
            empty_keys = [k for k in minimums if not k.strip()]
            if empty_keys:
                raise HTTPException(
                    status_code=422,
                    detail="tag_minimums 的键不能为空白内容标签",
                )
            quota_total = sum(minimums.values())
            if quota_total > body.max_items:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"tag_minimums 配额之和（{quota_total}）"
                        f"不能超过 max_items（{body.max_items}）"
                    ),
                )
            placeholders = ",".join("?" for _ in minimums)
            known = {
                r["content_tag"]
                for r in conn.execute(
                    f"SELECT DISTINCT content_tag FROM items "
                    f"WHERE content_tag IS NOT NULL AND content_tag <> '' "
                    f"AND content_tag IN ({placeholders})",
                    tuple(minimums),
                ).fetchall()
            }
            unknown = sorted(set(minimums) - known)
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"tag_minimums 包含题库不存在的内容标签：{unknown}",
                )

        conn.execute(
            """
            INSERT INTO sessions
                (id, theta, current_item_id, max_items, exposure_limit,
                 duration_seconds, expires_at, target_se, min_precision_items,
                 status, finish_reason, created_at, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
            """,
            (
                session_id,
                body.initial_theta,
                body.max_items,
                body.exposure_limit or DEFAULT_EXPOSURE_LIMIT,
                duration,
                expires_at,
                body.target_se,
                body.min_precision_items,
                now,
                now,
            ),
        )
        if minimums:
            conn.executemany(
                "INSERT INTO session_tag_minimums (session_id, content_tag, minimum) "
                "VALUES (?, ?, ?)",
                [(session_id, tag, qty) for tag, qty in minimums.items()],
            )
        row = get_session_or_404(conn, session_id)
    return serialize_session(row, 0, minimums)


@app.get("/api/sessions/{session_id}", response_model=SessionOut, tags=["sessions"])
def get_session(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    row = get_session_or_404(conn, session_id)
    n_answered = answered_count(conn, session_id)
    se = session_standard_error(conn, session_id, row["theta"])
    return serialize_session(
        row, n_answered, tag_minimums_for(conn, session_id), se
    )


@app.get(
    "/api/sessions/{session_id}/progress",
    response_model=ProgressOut,
    tags=["sessions"],
)
def session_progress(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ProgressOut:
    session = get_session_or_404(conn, session_id)
    n_answered = answered_count(conn, session_id)
    answered_ids = [
        r["item_id"]
        for r in conn.execute(
            "SELECT item_id FROM responses WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    ]
    current_item = None
    if session["current_item_id"]:
        item_row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (session["current_item_id"],)
        ).fetchone()
        if item_row is not None:
            current_item = serialize_item(item_row)
    bank_size = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    minimums = tag_minimums_for(conn, session_id)
    answered_tags = answered_by_tag(conn, session_id)
    se = session_standard_error(conn, session_id, session["theta"])
    # Report zero answered for every quota tag even if it has no response yet.
    tag_answered = {tag: answered_tags.get(tag, 0) for tag in minimums}
    tag_answered.update(answered_tags)
    return ProgressOut(
        session_id=session_id,
        status=session["status"],
        theta=session["theta"],
        answered_count=n_answered,
        max_items=session["max_items"],
        bank_size=bank_size,
        percent_complete=round(n_answered / session["max_items"] * 100, 1),
        exposure_limit=session["exposure_limit"],
        duration_seconds=session["duration_seconds"],
        expires_at=session["expires_at"],
        paused=session["status"] == "paused",
        paused_at=session["paused_at"],
        remaining_seconds=session_remaining_seconds(
            session, datetime.now(timezone.utc)
        ),
        tag_minimums=minimums,
        tag_answered=dict(sorted(tag_answered.items())),
        target_se=session["target_se"],
        min_precision_items=session["min_precision_items"],
        standard_error=se,
        finish_reason=session["finish_reason"],
        current_item=current_item,
        answered_item_ids=answered_ids,
    )


@app.get(
    "/api/sessions/{session_id}/responses",
    response_model=ResponseHistoryOut,
    tags=["sessions"],
)
def session_responses(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ResponseHistoryOut:
    session = get_session_or_404(conn, session_id)
    rows = conn.execute(
        "SELECT * FROM responses WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    return ResponseHistoryOut(
        session_id=session_id,
        status=session["status"],
        theta=session["theta"],
        count=len(rows),
        results=[serialize_response(r) for r in rows],
    )


# -------------------------------------------------------------------------
# Pause / resume
# -------------------------------------------------------------------------

def _pause_resume_payload(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> SessionOut:
    """Build the SessionOut body returned by the pause/resume endpoints."""
    n_answered = answered_count(conn, row["id"])
    se = session_standard_error(conn, row["id"], row["theta"])
    return serialize_session(
        row, n_answered, tag_minimums_for(conn, row["id"]), se
    )


@app.post(
    "/api/sessions/{session_id}/pause",
    response_model=SessionOut,
    tags=["adaptive"],
)
def pause_session(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    """Pause an active, not-yet-expired session.

    Only an ``active`` session can be paused; a session that is already
    paused or finished is rejected with 409.  When the session carries a
    time limit the remaining seconds (``expires_at - now``) are frozen into
    ``remaining_seconds`` and ``paused_at`` is recorded; the stored deadline
    is left untouched, so the time limit simply stops counting while paused.
    Sessions without a time limit can be paused and resumed too (their
    ``remaining_seconds`` stays ``null``).  An expired session cannot be kept
    alive by pausing: the deadline check runs first and finishes it as
    ``time_expired`` with a 409, exactly as /next and /responses do.
    """
    with transaction(conn):
        session = get_session_or_404(conn, session_id)

        expired = expire_if_due(conn, session, datetime.now(timezone.utc))
        if expired is not None:
            raise time_expired_409(expired)
        if session["status"] != "active":
            # Double pause (or pause on a finished session) -> 409.
            raise session_not_active_409(session, verb="暂停")

        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        remaining = session_remaining_seconds(session, now)
        conn.execute(
            """UPDATE sessions
               SET status = 'paused', paused_at = ?, remaining_seconds = ?,
                   updated_at = ?
               WHERE id = ?""",
            (now_text, remaining, now_text, session_id),
        )
        row = get_session_or_404(conn, session_id)
        payload = _pause_resume_payload(conn, row)
    return payload


@app.post(
    "/api/sessions/{session_id}/resume",
    response_model=SessionOut,
    tags=["adaptive"],
)
def resume_session(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    """Resume a paused session.

    Only a ``paused`` session can be resumed; resuming an active or finished
    session returns 409.  The UTC deadline is recomputed from the frozen
    ``remaining_seconds`` (``expires_at = now + remaining_seconds``), the
    pause markers are cleared and the session becomes ``active`` again with
    its pending item, stored answers, theta and counted exposures all
    intact — re-fetching the pending item via /next afterwards returns the
    same item without adding another exposure.  Time-limit-free sessions
    simply clear the pause markers.
    """
    with transaction(conn):
        session = get_session_or_404(conn, session_id)
        if session["status"] != "paused":
            raise session_not_active_409(session, verb="恢复")

        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        # NULL for a time-limit-free session (COALESCE keeps it NULL);
        # otherwise the stored deadline moves forward by exactly the seconds
        # that were frozen at pause time.
        conn.execute(
            """UPDATE sessions
               SET status = 'active',
                   expires_at = COALESCE(
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', ?,
                                '+' || remaining_seconds || ' seconds'),
                       expires_at
                   ),
                   paused_at = NULL,
                   remaining_seconds = NULL,
                   updated_at = ?
               WHERE id = ?""",
            (now_text, now_text, session_id),
        )
        row = get_session_or_404(conn, session_id)
        payload = _pause_resume_payload(conn, row)
    return payload


# -------------------------------------------------------------------------
# Adaptive item selection
# -------------------------------------------------------------------------

@app.post(
    "/api/sessions/{session_id}/next",
    response_model=NextItemOut,
    tags=["adaptive"],
)
def next_item(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> NextItemOut:
    """Select the next item.

    Eligibility: the item is enabled (``is_active = 1``),
    ``exposure_count < exposure_limit`` and the item has not already been
    answered in this session.  Selection: highest Fisher information at the
    current theta; ties break on the smallest item id, so the result is
    fully deterministic.

    A pending item that was withdrawn after being served is cleared inside
    this same transaction and a replacement is selected by the normal
    rules; the exposure counted when it was originally selected is not
    rolled back.  (If no eligible replacement exists the session finishes
    with the usual ``no_available_items`` /
    ``tag_minimums_unfulfillable`` reason.)

    When the session carries ``tag_minimums``, the per-tag deficit is
    computed from stored responses.  If the number of remaining answer slots
    equals the total deficit, candidates are restricted to deficient tags
    (same information/id tie-break); otherwise all eligible items remain in
    the pool.  If a deficient tag cannot reach its quota with the items still
    eligible, the session finishes with
    ``finish_reason = 'tag_minimums_unfulfillable'`` without serving an item
    (hence without adding an exposure).

    The whole read-check-select-write sequence runs inside one serialised
    write transaction.  That way a burst of concurrent calls on the same
    session is linearised: the first selects and counts one exposure, every
    later call finds the pending item and returns it unchanged.

    If the session has a ``duration_seconds`` limit whose UTC ``expires_at``
    has passed, the session is first atomically finished
    (``finish_reason = 'time_expired'``) with its pending item cleared, and
    the request is rejected with 409; no exposure is added.  A paused
    session is likewise rejected with 409, but its pending item is kept
    (pause never finishes or expires a session).
    """
    outcome: dict
    target_se: float | None = None
    min_precision_items: int | None = None
    se: float | None = None
    session: sqlite3.Row | None = None
    now_dt = datetime.now(timezone.utc)

    with transaction(conn):
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")

        # Case 0: the time limit has elapsed -> finish atomically and reject
        # the call with 409.  No item is selected (no new exposure) and no
        # response row is written; any pending item is cleared.
        now_dt = datetime.now(timezone.utc)
        expired = expire_if_due(conn, session, now_dt)
        if expired is not None:
            raise time_expired_409(expired)

        # Case 0b: the session is paused -> /next is rejected with 409 and no
        # exposure is added; the pending item is preserved untouched.
        if session["status"] == "paused":
            raise session_paused_409(session)

        target_se = session["target_se"]
        min_precision_items = session["min_precision_items"]
        n_answered = answered_count(conn, session_id)
        # /next never changes theta or the response set, so the current SE is
        # the same for every branch below.
        se = session_standard_error(conn, session_id, session["theta"])
        now = utc_now()

        # Resolve the pending item first.  A pending item that has been
        # withdrawn since it was served can no longer be answered: clear the
        # pending marker here, inside the same serialised transaction, and
        # fall through to the normal selection rules below.  The exposure
        # counted when the item was selected is NOT rolled back.
        pending_row: sqlite3.Row | None = None
        withdrawn_pending: str | None = None
        if session["current_item_id"]:
            candidate_pending = conn.execute(
                "SELECT * FROM items WHERE id = ?", (session["current_item_id"],)
            ).fetchone()
            if candidate_pending is not None and candidate_pending["is_active"]:
                pending_row = candidate_pending
            else:
                # Withdrawn (or, defensively, no longer existing) pending
                # item: drop the marker and re-select below.
                withdrawn_pending = session["current_item_id"]
                conn.execute(
                    """UPDATE sessions
                       SET current_item_id = NULL, updated_at = ?
                       WHERE id = ?""",
                    (now, session_id),
                )

        # Case 1: a served, unanswered (and still enabled) item is pending
        # -> return it as-is.
        if pending_row is not None:
            outcome = {
                "status": session["status"],
                "item_row": pending_row,
                "information": irt.information(
                    session["theta"],
                    pending_row["discrimination"],
                    pending_row["difficulty"],
                ),
                "exposure_limit": session["exposure_limit"],
                "answered_count": n_answered,
                "max_items": session["max_items"],
                "target_se": target_se,
                "min_precision_items": min_precision_items,
                "standard_error": se,
                "finish_reason": session["finish_reason"],
                "note": "pending_item: 该题尚未作答，重复请求不重复计曝光",
            }

        # Case 2: session already finished.
        elif session["status"] == "finished":
            outcome = {
                "status": "finished",
                "item_row": None,
                "information": None,
                "exposure_limit": session["exposure_limit"],
                "answered_count": n_answered,
                "max_items": session["max_items"],
                "target_se": target_se,
                "min_precision_items": min_precision_items,
                "standard_error": se,
                "finish_reason": session["finish_reason"],
                "note": "测验已结束",
            }

        # Case 3: item quota reached -> finish eagerly.
        elif n_answered >= session["max_items"]:
            conn.execute(
                """UPDATE sessions
                   SET status = 'finished', finish_reason = 'max_items',
                       updated_at = ?
                   WHERE id = ?""",
                (now, session_id),
            )
            outcome = {
                "status": "finished",
                "item_row": None,
                "information": None,
                "exposure_limit": session["exposure_limit"],
                "answered_count": n_answered,
                "max_items": session["max_items"],
                "target_se": target_se,
                "min_precision_items": min_precision_items,
                "standard_error": se,
                "finish_reason": "max_items",
                "note": "已达到最大题量",
            }

        else:
            # Candidate read under the write lock: exposure counts seen here
            # cannot change before we update them.  Withdrawn items
            # (is_active = 0) never take part in new selections.
            candidates = conn.execute(
                """
                SELECT * FROM items
                WHERE is_active = 1
                  AND exposure_count < ?
                  AND id NOT IN (
                      SELECT item_id FROM responses WHERE session_id = ?
                  )
                """,
                (session["exposure_limit"], session_id),
            ).fetchall()

            minimums = tag_minimums_for(conn, session_id)
            shortfalls = (
                tag_shortfalls(minimums, answered_by_tag(conn, session_id))
                if minimums
                else {}
            )
            total_shortfall = sum(shortfalls.values())
            slots_left = session["max_items"] - n_answered

            # A shortfall can only ever shrink (a quota-tagged answer covers
            # at most one deficit; a non-quota answer shrinks only the slots),
            # so total_shortfall <= slots_left always holds while selection
            # obeys the restriction below.  When the remaining answer slots
            # equal the deficit exactly, every remaining item must come from
            # a deficient tag.
            if minimums and total_shortfall == slots_left:
                candidates = [
                    c for c in candidates if c["content_tag"] in shortfalls
                ]

            # Case 4: no eligible item left in the (possibly restricted)
            # pool -> finish.  With quotas set this also covers the case in
            # which some deficient tag has no eligible item left while
            # non-quota items are still available: the session cannot be
            # completed either way.
            if not candidates:
                if minimums:
                    finish_reason = "tag_minimums_unfulfillable"
                    note = (
                        "欠额标签已没有满足曝光上限的候选题，无法补齐标签配额："
                        f"缺口 {shortfalls}"
                    )
                else:
                    finish_reason = "no_available_items"
                    note = "没有满足曝光上限的候选题"
                conn.execute(
                    """UPDATE sessions
                       SET status = 'finished',
                           finish_reason = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (finish_reason, now, session_id),
                )
                outcome = {
                    "status": "finished",
                    "item_row": None,
                    "information": None,
                    "exposure_limit": session["exposure_limit"],
                    "answered_count": n_answered,
                    "max_items": session["max_items"],
                    "target_se": target_se,
                    "min_precision_items": min_precision_items,
                    "standard_error": se,
                    "finish_reason": finish_reason,
                    "note": note,
                }

            else:
                # Even with slots to spare, if any deficient tag cannot
                # possibly reach its quota (not enough eligible items left
                # for that tag), finishing early is the only correct outcome:
                # no item is served and therefore no exposure is added.
                infeasible: dict[str, int] = {}
                if minimums:
                    available_by_tag = Counter(
                        c["content_tag"] for c in candidates
                    )
                    infeasible = {
                        tag: deficit
                        for tag, deficit in shortfalls.items()
                        if available_by_tag.get(tag, 0) < deficit
                    }
                if infeasible:
                    conn.execute(
                        """UPDATE sessions
                           SET status = 'finished',
                               finish_reason = 'tag_minimums_unfulfillable',
                               updated_at = ?
                           WHERE id = ?""",
                        (now, session_id),
                    )
                    outcome = {
                        "status": "finished",
                        "item_row": None,
                        "information": None,
                        "exposure_limit": session["exposure_limit"],
                        "answered_count": n_answered,
                        "max_items": session["max_items"],
                        "target_se": target_se,
                        "min_precision_items": min_precision_items,
                        "standard_error": se,
                        "finish_reason": "tag_minimums_unfulfillable",
                        "note": (
                            "欠额标签的可用题数不足以补齐配额，"
                            f"无法满足的缺口 {infeasible}"
                        ),
                    }

                # Case 5: select the maximum-information item (id tie-break),
                # count exactly one exposure and mark it pending atomically.
                else:
                    theta = session["theta"]
                    best = min(
                        candidates,
                        key=lambda r: (
                            -irt.information(
                                theta, r["discrimination"], r["difficulty"]
                            ),
                            r["id"],
                        ),
                    )
                    info = irt.information(
                        theta, best["discrimination"], best["difficulty"]
                    )
                    conn.execute(
                        "UPDATE items SET exposure_count = exposure_count + 1 "
                        "WHERE id = ?",
                        (best["id"],),
                    )
                    conn.execute(
                        "UPDATE sessions SET current_item_id = ?, updated_at = ? "
                        "WHERE id = ?",
                        (best["id"], now, session_id),
                    )
                    item_row = conn.execute(
                        "SELECT * FROM items WHERE id = ?", (best["id"],)
                    ).fetchone()
                    outcome = {
                        "status": "active",
                        "item_row": item_row,
                        "information": info,
                        "exposure_limit": session["exposure_limit"],
                        "answered_count": n_answered,
                        "max_items": session["max_items"],
                        "target_se": target_se,
                        "min_precision_items": min_precision_items,
                        "standard_error": se,
                        "finish_reason": None,
                        "note": None,
                    }

    if withdrawn_pending is not None:
        # The pending item was withdrawn: it was cleared and (when possible)
        # replaced inside the transaction above.  Surface that in the note;
        # the exposure counted at its original selection stays as-is.
        cleared_note = (
            f"原挂起题 {withdrawn_pending} 已撤回：已在同一事务内清除挂起项"
            "并按既有选题规则重选（该题已计曝光不回退）"
        )
        outcome["note"] = (
            f"{outcome['note']}；{cleared_note}"
            if outcome["note"]
            else cleared_note
        )

    item_row = outcome["item_row"]
    return NextItemOut(
        session_id=session_id,
        status=outcome["status"],
        item=serialize_item(item_row) if item_row is not None else None,
        information=outcome["information"],
        exposure_count=item_row["exposure_count"] if item_row is not None else None,
        exposure_limit=outcome["exposure_limit"],
        answered_count=outcome["answered_count"],
        max_items=outcome["max_items"],
        paused=outcome["status"] == "paused",
        paused_at=session["paused_at"] if outcome["status"] == "paused" else None,
        remaining_seconds=session_remaining_seconds(session, now_dt),
        target_se=outcome["target_se"],
        min_precision_items=outcome["min_precision_items"],
        standard_error=outcome["standard_error"],
        finish_reason=outcome["finish_reason"],
        note=outcome["note"],
    )


# -------------------------------------------------------------------------
# Answer submission (idempotent, one Newton update)
# -------------------------------------------------------------------------

@app.post(
    "/api/sessions/{session_id}/responses",
    response_model=ResponseOut,
    status_code=201,
    tags=["adaptive"],
)
def submit_response(
    session_id: str,
    body: ResponseCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> ResponseOut | JSONResponse:
    """Score one answer and apply exactly one Newton update.

    Idempotency contract: the client-generated ``response_id`` is the
    dedupe key.  The deadline check is the *first* statement inside the
    serialised write transaction: once ``expires_at`` has passed the session
    is atomically finished (``time_expired``) with its pending item cleared,
    and the call is rejected with 409 — no exposure is added and no response
    row is written.  Afterwards the idempotency lookup is checked under the
    same write lock, so even a burst of concurrent retries observes the
    first committed record and is replayed (HTTP 200) instead of being
    scored a second time.  A paused session rejects *new* answers with 409
    (the pending item is preserved), but a replayed ``response_id`` is still
    served by the idempotency lookup above.  A *new* answer for a withdrawn
    item (``is_active = 0``) is likewise rejected with 409 — no response
    row is written and theta is not updated — while a replayed
    ``response_id`` still returns the first stored result.

    After the theta update the optional precision stop is evaluated in the
    same transaction: when the session has a ``target_se``, the summed 2PL
    Fisher information of every answered item is recomputed at the new theta
    and the session finishes with ``finish_reason = 'precision_reached'``
    once the answered count reaches ``min_precision_items``, the standard
    error is at most ``target_se`` and all tag quotas are met.  A replayed
    request takes the idempotency branch above and never re-evaluates it.
    """
    response_key = body.response.strip().upper()
    stored_row: sqlite3.Row | None = None
    replay = False

    with transaction(conn):
        # 1) Deadline check, first inside the serialised write transaction:
        #    expiry finishes the session atomically and rejects the call;
        #    nothing else (exposure, response row) is written.
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        expired = expire_if_due(conn, session, datetime.now(timezone.utc))
        if expired is not None:
            raise time_expired_409(expired)

        # 2) Idempotency replay, checked after acquiring the write lock.
        existing = conn.execute(
            "SELECT * FROM responses WHERE id = ?", (body.response_id,)
        ).fetchone()
        if existing is not None:
            if existing["session_id"] != session_id or existing["item_id"] != body.item_id:
                raise HTTPException(
                    status_code=409,
                    detail="response_id 已被其他会话或题目使用",
                )
            stored_row, replay = existing, True
        else:
            # 3) Validate the pending item state (existence, deadline and
            #    finished status have already been checked above).
            if session["status"] == "paused":
                # A *new* answer is rejected while paused; the pending item
                # and every stored answer are preserved.  An idempotent
                # replay took the branch above and is still served.
                raise session_paused_409(session)
            if session["status"] == "finished":
                raise HTTPException(status_code=409, detail="测验已结束")
            if session["current_item_id"] != body.item_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "item_id 与当前待作答题不一致或该题已作答："
                        "请先调用 "
                        f"POST /api/sessions/{session_id}/next 获取题目；"
                        "如为重试请求，请复用原 response_id"
                    ),
                )

            item = conn.execute(
                "SELECT * FROM items WHERE id = ?", (body.item_id,)
            ).fetchone()
            if item is None:
                raise HTTPException(status_code=404, detail="item not found")
            if not item["is_active"]:
                # A withdrawn item accepts no new answers: no response row is
                # written and theta is left untouched.  A replayed
                # response_id took the idempotency branch above and is still
                # served; the pending marker is cleared by the next /next.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"题目 {body.item_id} 已撤回，不再接受新的作答；"
                        "请调用 "
                        f"POST /api/sessions/{session_id}/next 重新选题"
                    ),
                )

            option_keys = {opt["key"] for opt in parse_options(item["options"])}
            if response_key not in option_keys:
                raise HTTPException(
                    status_code=422,
                    detail=f"无效作答选项：{body.response!r}，可选 {sorted(option_keys)}",
                )

            # 4) Score and apply exactly one Newton-Raphson update.
            score = 1 if response_key == item["answer"] else 0
            theta_before = session["theta"]
            theta_after, p_correct, _, info = irt.newton_step(
                theta_before, item["discrimination"], item["difficulty"], score
            )
            now = utc_now()
            exposure_at_select = item["exposure_count"]

            try:
                conn.execute(
                    """
                    INSERT INTO responses (
                        id, session_id, item_id, response, score, correct_answer,
                        theta_before, theta_after, p_correct,
                        information_at_theta, exposure_count_at_select, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.response_id,
                        session_id,
                        body.item_id,
                        response_key,
                        score,
                        item["answer"],
                        theta_before,
                        theta_after,
                        p_correct,
                        info,
                        exposure_at_select,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Database-level backstop for a concurrent key collision.
                conn.rollback()
                racer = conn.execute(
                    "SELECT * FROM responses WHERE id = ?", (body.response_id,)
                ).fetchone()
                if racer is not None:
                    if racer["session_id"] != session_id or racer["item_id"] != body.item_id:
                        raise HTTPException(
                            status_code=409,
                            detail="response_id 已被其他会话或题目使用",
                        )
                    # Re-read outside the rolled-back transaction below.
                    stored_row, replay = racer, True
                else:
                    raise HTTPException(status_code=409, detail="重复作答冲突")
            else:
                # 5) Advance session state; finish eagerly at max_items, or
                #    when the optional precision stop rule is satisfied.
                conn.execute(
                    """UPDATE sessions
                       SET theta = ?, current_item_id = NULL, updated_at = ?
                       WHERE id = ?""",
                    (theta_after, now, session_id),
                )
                total = conn.execute(
                    "SELECT COUNT(*) FROM responses WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                if total >= session["max_items"]:
                    # A full answer sheet wins over every other stop rule.
                    conn.execute(
                        """UPDATE sessions
                           SET status = 'finished', finish_reason = 'max_items',
                               updated_at = ?
                           WHERE id = ?""",
                        (now, session_id),
                    )
                elif session["target_se"] is not None:
                    # Optional precision stop: re-evaluate the summed 2PL
                    # Fisher information of *all* answered items at the new
                    # theta.  The session ends in this same transaction only
                    # when the answer count has reached the configured floor,
                    # SE <= target_se and every tag quota is already met.  A
                    # zero/non-positive total gives SE = NULL and can never
                    # fire; otherwise the selection flow continues unchanged.
                    se = session_standard_error(conn, session_id, theta_after)
                    quota_met = not tag_shortfalls(
                        tag_minimums_for(conn, session_id),
                        answered_by_tag(conn, session_id),
                    )
                    if (
                        se is not None
                        and total >= session["min_precision_items"]
                        and se <= session["target_se"]
                        and quota_met
                    ):
                        conn.execute(
                            """UPDATE sessions
                               SET status = 'finished',
                                   finish_reason = 'precision_reached',
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, session_id),
                        )
                stored_row = conn.execute(
                    "SELECT * FROM responses WHERE id = ?", (body.response_id,)
                ).fetchone()

    payload = serialize_response(stored_row).model_dump()
    return JSONResponse(status_code=200 if replay else 201, content=payload)
