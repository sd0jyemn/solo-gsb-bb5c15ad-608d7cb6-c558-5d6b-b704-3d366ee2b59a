"""答卷雷同复核：在已结束会话之间查找同错作答模式。

``GET /api/sessions/{session_id}/similarity`` 把一个**已结束**目标会话与其余
已结束会话按 ``item_id`` 对齐双方历史作答。只有双方在同一道题上都答错、且
选择了**同一个错误选项**才计为一次匹配；任一方答对都不计（两份都正确的答卷
没有雷同可言）。

每个同错匹配的权重为

    w(item, option) = log((E_item + 2) / (N_item_option + 1))

其中 ``E_item`` 是已结束会话在该题上的错误作答总数，``N_item_option`` 是其中
选择了该错误选项的次数——选错越普遍的选项权重越低，少数派错选权重更高。候选
得分 = 匹配权重之和 / 共答题数；共答题数指双方都作答过（不论对错）的题数，
候选至少共答 ``min_shared`` 道题才入选。

所有统计都只读取 ``responses`` 中作答时冻结的 ``score`` 与 ``response``：
``score`` 决定当时答对与否，``response`` 是当时选择的选项，因此之后通过批量
导入修改题库答案不会改变雷同复核结果（活跃/暂停中会话的作答同样不纳入统计）。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

# SQLite 默认最多 999 个宿主参数；所有 IN 列表都分块到此值以下。
_PARAM_CHUNK = 500


@dataclass(frozen=True)
class SimilarSession:
    """一个雷同候选的复核结果。"""

    session_id: str
    shared_count: int
    same_wrong_count: int
    score: float


def _in_chunks(values: list[str]) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(placeholders, chunk)`` pairs keeping each IN list small."""
    for start in range(0, len(values), _PARAM_CHUNK):
        chunk = values[start : start + _PARAM_CHUNK]
        yield ",".join("?" for _ in chunk), chunk


def _target_responses(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, sqlite3.Row]:
    """Target session's frozen answers keyed by ``item_id``.

    Only the frozen ``response`` / ``score`` columns are used, so later bank
    edits (including a changed correct answer) cannot affect the result.
    """
    return {
        row["item_id"]: row
        for row in conn.execute(
            "SELECT item_id, response, score FROM responses "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    }


def _match_weights(
    conn: sqlite3.Connection, target_wrong: dict[str, str]
) -> dict[tuple[str, str], float]:
    """Weight ``log((wrong_total + 2) / (option_count + 1))`` for each of the
    target session's own ``(item, wrong option)`` pairs.

    Both the per-item wrong-answer total and the per-option choice count are
    counted over finished sessions only, straight from frozen response rows.
    """
    wrong_item_ids = sorted(target_wrong)
    wrong_totals: dict[str, int] = {}
    option_counts: dict[tuple[str, str], int] = {}
    for placeholders, chunk in _in_chunks(wrong_item_ids):
        for row in conn.execute(
            f"""
            SELECT r.item_id AS item_id, COUNT(*) AS n
              FROM responses r
              JOIN sessions s ON s.id = r.session_id
             WHERE s.status = 'finished'
               AND r.score = 0
               AND r.item_id IN ({placeholders})
             GROUP BY r.item_id
            """,
            chunk,
        ).fetchall():
            wrong_totals[row["item_id"]] = row["n"]
        for row in conn.execute(
            f"""
            SELECT r.item_id AS item_id, r.response AS response, COUNT(*) AS n
              FROM responses r
              JOIN sessions s ON s.id = r.session_id
             WHERE s.status = 'finished'
               AND r.score = 0
               AND r.item_id IN ({placeholders})
             GROUP BY r.item_id, r.response
            """,
            chunk,
        ).fetchall():
            option_counts[(row["item_id"], row["response"])] = row["n"]

    weights: dict[tuple[str, str], float] = {}
    for item_id, option in target_wrong.items():
        total = wrong_totals.get(item_id, 0)
        chosen = option_counts.get((item_id, option), 0)
        weights[(item_id, option)] = math.log((total + 2) / (chosen + 1))
    return weights


def find_similar_sessions(
    conn: sqlite3.Connection,
    session_id: str,
    min_shared: int,
    limit: int,
) -> list[SimilarSession]:
    """Rank other finished sessions by shared wrong-option patterns.

    The target itself and active/paused sessions are excluded; candidates must
    share at least *min_shared* answered items (both answered the item, right
    or wrong).  Rows are ordered by score desc, same-wrong count desc, session
    id asc and capped at *limit*.  An empty list means no eligible candidate.
    """
    target = _target_responses(conn, session_id)
    if not target:
        return []
    target_items = sorted(target)
    target_wrong = {
        item_id: row["response"]
        for item_id, row in target.items()
        if row["score"] == 0
    }
    weights = _match_weights(conn, target_wrong)

    # Co-answered items per other finished session.  Correct and wrong target
    # items are both included: an item counts as shared when both sessions
    # answered it, regardless of either outcome.
    shared_counts: dict[str, int] = {}
    for placeholders, chunk in _in_chunks(target_items):
        for row in conn.execute(
            f"""
            SELECT r.session_id AS session_id, COUNT(*) AS n
              FROM responses r
              JOIN sessions s ON s.id = r.session_id
             WHERE s.status = 'finished'
               AND r.session_id != ?
               AND r.item_id IN ({placeholders})
             GROUP BY r.session_id
            """,
            (session_id, *chunk),
        ).fetchall():
            shared_counts[row["session_id"]] = (
                shared_counts.get(row["session_id"], 0) + row["n"]
            )
    candidate_ids = [sid for sid, n in shared_counts.items() if n >= min_shared]
    if not candidate_ids:
        return []

    # Same-wrong matches: candidate chose the target's frozen wrong option.
    # Both filters (candidate id and target-wrong item) are chunked below the
    # SQLite host-parameter cap.
    same_wrong: dict[str, int] = {}
    weight_sum: dict[str, float] = {}
    wrong_item_ids = sorted(target_wrong)
    for sid_start in range(0, len(candidate_ids), _PARAM_CHUNK):
        sid_chunk = candidate_ids[sid_start : sid_start + _PARAM_CHUNK]
        sid_placeholders = ",".join("?" for _ in sid_chunk)
        for item_placeholders, item_chunk in _in_chunks(wrong_item_ids):
            rows = conn.execute(
                f"""
                SELECT r.session_id AS session_id, r.item_id AS item_id,
                       r.response AS response
                  FROM responses r
                 WHERE r.score = 0
                   AND r.session_id IN ({sid_placeholders})
                   AND r.item_id IN ({item_placeholders})
                """,
                (*sid_chunk, *item_chunk),
            ).fetchall()
            for row in rows:
                if target_wrong.get(row["item_id"]) == row["response"]:
                    sid = row["session_id"]
                    same_wrong[sid] = same_wrong.get(sid, 0) + 1
                    weight_sum[sid] = weight_sum.get(
                        sid, 0.0
                    ) + weights[(row["item_id"], row["response"])]

    results = [
        SimilarSession(
            session_id=sid,
            shared_count=shared_counts[sid],
            same_wrong_count=same_wrong.get(sid, 0),
            score=weight_sum.get(sid, 0.0) / shared_counts[sid],
        )
        for sid in candidate_ids
    ]
    results.sort(key=lambda r: (-r.score, -r.same_wrong_count, r.session_id))
    return results[:limit]
