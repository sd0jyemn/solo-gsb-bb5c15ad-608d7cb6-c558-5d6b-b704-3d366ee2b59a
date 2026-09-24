"""End-to-end demo using only the Python standard library.

Run with the API already listening on http://127.0.0.1:8000:

    python examples/demo.py

It creates a time-limited session, runs a mini adaptive quiz, demonstrates
idempotent retry with a repeated response_id, shows /next de-duplication,
print session progress and the answer history, and demonstrates the
duration_seconds validation (60..86400).  It then demonstrates content-tag
minimum quotas (``tag_minimums``), including the
``tag_minimums_unfulfillable`` finish reason when a quota cannot be met,
the optional precision stop rule (``target_se`` /
``min_precision_items``, ``finish_reason = precision_reached``), and
finally pause/resume (frozen remaining time, 409 while paused, pending
item and exposures preserved after resume, unlimited-time sessions).
The last section demonstrates item withdrawal
(``PATCH /api/items/{id}/availability``): withdrawn items leave new
selections, a withdrawn pending item is cleared and re-selected by /next
without rolling back its exposure, new answers for it are rejected with
409 while a stored ``response_id`` replay still returns the first result.
"""

from __future__ import annotations

import json
import math
import random
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def expect_http_error(
    method: str, path: str, payload: dict | None = None
) -> None:
    """Show that a request is rejected (used for validation / 409 cases)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        print(f"  -> 被拒绝 HTTP {exc.code}: {json.dumps(body['detail'], ensure_ascii=False)}")
    else:
        raise AssertionError("应当被服务端拒绝")


def show(label: str, obj: object) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    health = call("GET", "/health")
    show("GET /health", health)

    # duration_seconds 必须在 60..86400 之间；越界由 Pydantic 以 422 拒绝。
    expect_http_error(
        "POST", "/api/sessions",
        {"initial_theta": 0.0, "max_items": 8, "duration_seconds": 30},
    )

    # 本场测验限时 600 秒；响应中 duration_seconds 与 UTC expires_at 均会返回。
    session = call(
        "POST", "/api/sessions",
        {"initial_theta": 0.0, "max_items": 8, "exposure_limit": 5,
         "duration_seconds": 600},
    )
    sid = session["id"]
    show("POST /api/sessions", session)
    assert session["duration_seconds"] == 600
    assert session["expires_at"], "设置时限后应返回 UTC 截止时间"
    print(f"  -> 时限 600 秒，UTC 截止时间 expires_at = {session['expires_at']}")
    print("  -> 到期后 /next 与提交答案会返回 409，会话置为 finished(time_expired)")

    rng = random.Random(7)
    theta = session["theta"]

    for turn in range(1, 6):
        chosen = call("POST", f"/api/sessions/{sid}/next")
        item = chosen["item"]
        show(
            f"第 {turn} 题 (POST .../next)",
            {
                "item_id": item["id"],
                "stem": item["stem"],
                "information": round(chosen["information"], 4),
                "exposure_count": chosen["exposure_count"],
            },
        )

        # Re-calling /next before answering must return the SAME item and
        # must not increment exposure again.
        again = call("POST", f"/api/sessions/{sid}/next")
        assert again["item"]["id"] == item["id"], "重复 /next 应返回同一题"
        assert again["exposure_count"] == chosen["exposure_count"], \
            "重复 /next 不应重复计曝光"

        # Simulated examinee using the same 2PL model: answer correctly with
        # probability P(theta), otherwise pick a random distractor key.
        z = item["discrimination"] * (theta - item["difficulty"])
        p = 1 / (1 + math.exp(-z))
        keys = [opt["key"] for opt in item["options"]]
        # The served item hides the answer, but the demo needs to "be right"
        # sometimes: read it from the admin endpoint, else pick a distractor.
        detail = call("GET", f"/api/items/{item['id']}")
        if rng.random() < p:
            answer = detail["answer"]
        else:
            answer = rng.choice([k for k in keys if k != detail["answer"]])
        response_id = f"demo-resp-{turn}-sid-{sid[:8]}"

        result = call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": response_id, "item_id": item["id"], "response": answer},
        )
        theta = result["theta_after"]
        show(
            f"提交作答 (POST .../responses)",
            {
                "response_id": result["id"],
                "item_id": result["item_id"],
                "response": result["response"],
                "correct": result["correct"],
                "theta_before": round(result["theta_before"], 4),
                "theta_after": round(result["theta_after"], 4),
            },
        )

        # Idempotency: replay the exact same request body.
        replay = call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": response_id, "item_id": item["id"], "response": answer},
        )
        assert replay["created_at"] == result["created_at"], "应返回首次记录"
        assert replay["theta_after"] == result["theta_after"], "不应重复更新"
        print(f"  -> 重复 response_id 返回首次结果，能力值未二次更新 ✔")

    progress = call("GET", f"/api/sessions/{sid}/progress")
    show("GET .../progress (会话进度)", {
        "status": progress["status"],
        "theta": round(progress["theta"], 4),
        "answered_count": progress["answered_count"],
        "percent_complete": progress["percent_complete"],
        "duration_seconds": progress["duration_seconds"],
        "expires_at": progress["expires_at"],
        "answered_item_ids": progress["answered_item_ids"],
        "current_item": (progress["current_item"] or {}).get("id"),
    })

    history = call("GET", f"/api/sessions/{sid}/responses")
    show("GET .../responses (答题记录)", {
        "count": history["count"],
        "results": [
            {
                "item_id": r["item_id"],
                "correct": r["correct"],
                "theta_after": round(r["theta_after"], 4),
            }
            for r in history["results"]
        ],
    })

    bank = call("GET", "/api/items?limit=3")
    show("GET /api/items?limit=3 (题库与曝光)", bank)

    demo_tag_minimums()
    demo_precision_stop()
    demo_pause_resume()
    demo_withdrawal()


def demo_pause_resume() -> None:
    """Show pause/resume: frozen clock, 409 while paused, state preserved."""
    # --- 限时会话：暂停冻结剩余秒数，恢复时重算 UTC 截止时间 ---
    session = call(
        "POST", "/api/sessions",
        {"max_items": 5, "exposure_limit": 10_000, "duration_seconds": 600},
    )
    sid = session["id"]
    chosen = call("POST", f"/api/sessions/{sid}/next")
    item = chosen["item"]
    print(f"  -> 取到第 1 题 {item['id']}（全局曝光 {chosen['exposure_count']}），暂不作答")

    paused = call("POST", f"/api/sessions/{sid}/pause")
    show("POST .../pause（限时会话）", {
        "status": paused["status"],
        "paused": paused["paused"],
        "paused_at": paused["paused_at"],
        "remaining_seconds": paused["remaining_seconds"],
        "expires_at": paused["expires_at"],
        "current_item_id": paused["current_item_id"],
    })
    assert paused["status"] == "paused" and paused["paused"] is True
    assert 595 <= paused["remaining_seconds"] <= 600
    frozen = paused["remaining_seconds"]

    # 暂停期间取题与新作答都被 409 拒绝；重复暂停也是 409。
    expect_http_error("POST", f"/api/sessions/{sid}/next")
    expect_http_error(
        "POST", f"/api/sessions/{sid}/responses",
        {"response_id": "demo-pause-new", "item_id": item["id"], "response": "A"},
    )
    expect_http_error("POST", f"/api/sessions/{sid}/pause")
    print("  -> 暂停期间 /next、新作答、重复暂停均返回 409 ✔")

    # 挂起题与剩余秒数在暂停期间保持不变。
    progress = call("GET", f"/api/sessions/{sid}/progress")
    assert progress["paused"] is True
    assert progress["current_item"]["id"] == item["id"]
    assert progress["remaining_seconds"] == frozen
    assert call("GET", f"/api/items/{item['id']}")["exposure_count"] == \
        chosen["exposure_count"], "暂停被拒后不应增加曝光"
    print("  -> 暂停查询显示 paused 与冻结的 remaining_seconds，挂起题/曝光不变 ✔")

    resumed = call("POST", f"/api/sessions/{sid}/resume")
    show("POST .../resume（恢复）", {
        "status": resumed["status"],
        "paused": resumed["paused"],
        "paused_at": resumed["paused_at"],
        "remaining_seconds": resumed["remaining_seconds"],
        "expires_at": resumed["expires_at"],
        "current_item_id": resumed["current_item_id"],
    })
    assert resumed["status"] == "active" and resumed["paused"] is False
    assert resumed["paused_at"] is None
    assert resumed["expires_at"] != session["expires_at"], "截止时间应已重算"
    assert resumed["remaining_seconds"] <= frozen
    # 重复恢复 -> 409。
    expect_http_error("POST", f"/api/sessions/{sid}/resume")
    print("  -> 恢复后 expires_at 按冻结剩余秒数重算，重复恢复返回 409 ✔")

    # 恢复后挂起题仍是同一题，重复 /next 不重复计曝光。
    again = call("POST", f"/api/sessions/{sid}/next")
    assert again["item"]["id"] == item["id"]
    assert again["exposure_count"] == chosen["exposure_count"], \
        "恢复后重复取待答题不应再次计曝光"
    print("  -> 恢复后保留原待答题，重复 /next 返回同一题且不增加曝光 ✔")

    # 恢复后可正常作答，已有进度延续。
    detail = call("GET", f"/api/items/{item['id']}")
    answered = call(
        "POST", f"/api/sessions/{sid}/responses",
        {"response_id": f"demo-pause-{sid[:8]}-1",
         "item_id": item["id"], "response": detail["answer"]},
    )
    assert answered["correct"] is True
    progress = call("GET", f"/api/sessions/{sid}/progress")
    assert progress["answered_count"] == 1
    assert progress["answered_item_ids"] == [item["id"]]
    print("  -> 恢复后作答成功，答题记录与进度延续 ✔")

    # --- 无时限会话同样可暂停/恢复，remaining_seconds 恒为 null ---
    untimed = call(
        "POST", "/api/sessions",
        {"max_items": 3, "exposure_limit": 10_000},
    )
    assert untimed["expires_at"] is None
    p = call("POST", f"/api/sessions/{untimed['id']}/pause")
    assert p["status"] == "paused" and p["remaining_seconds"] is None
    expect_http_error("POST", f"/api/sessions/{untimed['id']}/next")
    r = call("POST", f"/api/sessions/{untimed['id']}/resume")
    assert r["status"] == "active" and r["remaining_seconds"] is None
    assert call("POST", f"/api/sessions/{untimed['id']}/next")["item"]
    print("  -> 无时限会话也能暂停/恢复，remaining_seconds 始终为 null ✔")

    # --- 已结束会话不能暂停或恢复（均 409）；到期会话也不能靠暂停续命 ---
    one = call(
        "POST", "/api/sessions",
        {"max_items": 1, "exposure_limit": 10_000},
    )
    oid = one["id"]
    oi = call("POST", f"/api/sessions/{oid}/next")["item"]
    od = call("GET", f"/api/items/{oi['id']}")
    call(
        "POST", f"/api/sessions/{oid}/responses",
        {"response_id": f"demo-pause-done-{oid[:8]}",
         "item_id": oi["id"], "response": od["answer"]},
    )
    assert call("GET", f"/api/sessions/{oid}")["status"] == "finished"
    expect_http_error("POST", f"/api/sessions/{oid}/pause")
    expect_http_error("POST", f"/api/sessions/{oid}/resume")
    print("  -> 已结束会话暂停/恢复均返回 409 ✔")
    print("  -> 到期会话调用暂停会与 /next、/responses 一样被直接结束为 "
          "time_expired（409），无法借暂停续命（详见 README）")



def demo_precision_stop() -> None:
    """Show target_se/min_precision_items and the precision_reached finish."""
    # 校验：两个参数必须成对出现；target_se 须为正有限数；
    # min_precision_items 不能大于 max_items -> 均返回 422。
    expect_http_error(
        "POST", "/api/sessions", {"target_se": 0.5},
    )
    expect_http_error(
        "POST", "/api/sessions", {"min_precision_items": 2},
    )
    expect_http_error(
        "POST", "/api/sessions",
        {"target_se": 0, "min_precision_items": 2},
    )
    expect_http_error(
        "POST", "/api/sessions",
        {"max_items": 3, "target_se": 0.5, "min_precision_items": 4},
    )

    # 目标标准误 0.8、至少作答 3 题后才允许触发精度停测。
    # exposure_limit 放大到 10000：避免与本场其他演示共享的全局曝光计数
    # 影响选题，让本段每次运行的轨迹都一致。
    session = call(
        "POST", "/api/sessions",
        {"max_items": 12, "exposure_limit": 10_000,
         "target_se": 0.8, "min_precision_items": 3},
    )
    sid = session["id"]
    assert session["target_se"] == 0.8
    assert session["min_precision_items"] == 3
    assert session["standard_error"] is None
    print("  -> 创建精度停测会话：target_se=0.8，min_precision_items=3")

    # 模拟一个真实能力固定在 theta=0 的考生：答对概率始终按 theta=0 计算，
    # 这样 θ 估计会在 0 附近波动，标准误会随作答题数稳定下降。
    rng = random.Random(123)
    theta_true = 0.0
    trace: list[dict] = []
    for turn in range(1, 13):
        chosen = call("POST", f"/api/sessions/{sid}/next")
        assert chosen["target_se"] == 0.8
        assert chosen["min_precision_items"] == 3
        if chosen["item"] is None:
            break
        item = chosen["item"]
        detail = call("GET", f"/api/items/{item['id']}")
        keys = [opt["key"] for opt in item["options"]]
        z = item["discrimination"] * (theta_true - item["difficulty"])
        p = 1 / (1 + math.exp(-z))
        answer = (
            detail["answer"]
            if rng.random() < p
            else rng.choice([k for k in keys if k != detail["answer"]])
        )
        call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": f"demo-se-{turn}-sid-{sid[:8]}",
             "item_id": item["id"], "response": answer},
        )
        progress = call("GET", f"/api/sessions/{sid}/progress")
        trace.append({
            "turn": turn,
            "answered": progress["answered_count"],
            "standard_error": (
                round(progress["standard_error"], 4)
                if progress["standard_error"] is not None else None
            ),
            "status": progress["status"],
            "finish_reason": progress["finish_reason"],
        })
        if progress["status"] == "finished":
            assert progress["finish_reason"] == "precision_reached"
            assert progress["answered_count"] >= 3
            assert progress["standard_error"] <= 0.8 + 1e-12
            break
    else:  # pragma: no cover - the target is deliberately easy to reach
        raise AssertionError("精度停测应当被触发")

    show("精度停测（SE 轨迹，到达 0.8 且已答 >=3 题即停）", {
        "target_se": 0.8,
        "min_precision_items": 3,
        "trace": trace,
    })
    n_stopped = trace[-1]["answered"]
    print(
        f"  -> 第 {n_stopped} 题作答后 SE={trace[-1]['standard_error']} "
        "<= 0.8，会话以 precision_reached 结束 ✔"
    )

    # 重复提交最后一次 response_id：回放首次结果，不重复触发状态变化。
    history = call("GET", f"/api/sessions/{sid}/responses")
    last = history["results"][-1]
    replay = call(
        "POST", f"/api/sessions/{sid}/responses",
        {"response_id": last["id"], "item_id": last["item_id"],
         "response": last["response"]},
    )
    assert replay["created_at"] == last["created_at"]
    progress = call("GET", f"/api/sessions/{sid}/progress")
    assert progress["status"] == "finished"
    assert progress["finish_reason"] == "precision_reached"
    assert progress["answered_count"] == n_stopped
    print("  -> 重复 response_id 回放，状态与已答数不变 ✔")


def demo_tag_minimums() -> None:
    """Show tag_minimums: restricted endgame and the unfulfillable finish."""
    # 校验：配额之和不能超过 max_items -> 422
    expect_http_error(
        "POST", "/api/sessions",
        {"max_items": 3, "tag_minimums": {"算术": 2, "代数": 2}},
    )
    # 校验：键必须是题库现有非空 content_tag -> 422
    expect_http_error(
        "POST", "/api/sessions",
        {"max_items": 3, "tag_minimums": {"不存在的标签": 1}},
    )

    # 6 道题全部要求来自「数论」：剩余答题位始终等于缺口总数，
    # 因此每题都必须在欠额标签内按信息量/ID 规则选出。
    session = call(
        "POST", "/api/sessions",
        {"max_items": 6, "exposure_limit": 50, "tag_minimums": {"数论": 6}},
    )
    sid = session["id"]
    assert session["tag_minimums"] == {"数论": 6}
    tags: list[str] = []
    for turn in range(1, 7):
        chosen = call("POST", f"/api/sessions/{sid}/next")
        item = chosen["item"]
        tags.append(item["content_tag"])
        detail = call("GET", f"/api/items/{item['id']}")
        call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": f"demo-tag-{sid[:8]}-{turn}",
             "item_id": item["id"], "response": detail["answer"]},
        )
    progress = call("GET", f"/api/sessions/{sid}/progress")
    show("tag_minimums 配额会话（数论 x6）", {
        "served_tags": tags,
        "status": progress["status"],
        "finish_reason": progress["finish_reason"],
        "tag_minimums": progress["tag_minimums"],
        "tag_answered": progress["tag_answered"],
    })
    assert all(t == "数论" for t in tags), tags
    assert progress["finish_reason"] == "max_items"

    # 不可补齐：要求 1 道「概率统计」题（exposure_limit=1）。随后用若干
    # 一次性配额会话把该标签题全部曝光到上限；若本库此前已跑过演示，第一
    # 个消耗会话就会直接结束，循环同样终止（重复运行结果稳定）。
    quota_session = call(
        "POST", "/api/sessions",
        {"max_items": 3, "exposure_limit": 1,
         "tag_minimums": {"概率统计": 1}},
    )
    while True:
        burner = call(
            "POST", "/api/sessions",
            {"max_items": 1, "exposure_limit": 1,
             "tag_minimums": {"概率统计": 1}},
        )
        picked = call("POST", f"/api/sessions/{burner['id']}/next")
        if picked["item"] is None:
            assert picked["finish_reason"] == "tag_minimums_unfulfillable"
            break
        assert picked["item"]["content_tag"] == "概率统计"
    done = call("POST", f"/api/sessions/{quota_session['id']}/next")
    show("欠额标签已无可用题 -> 会话结束", {
        "status": done["status"],
        "finish_reason": done["finish_reason"],
        "item": done["item"],
        "note": done["note"],
    })
    assert done["status"] == "finished"
    assert done["finish_reason"] == "tag_minimums_unfulfillable"
    assert done["item"] is None, "结束时不额外选题/曝光"
    print("  -> 欠额标签题数不足时以 tag_minimums_unfulfillable 结束，未增加曝光 ✔")


def demo_withdrawal() -> None:
    """Show item withdrawal: PATCH availability, pending re-select, 409."""
    # 三道专用演示题：区分度远高于演示题库（a<=1.8），在 theta=0 的新会话里
    # 必然按 DEMO-WD-1 -> DEMO-WD-2 -> DEMO-WD-3 的顺序被选中，结果确定。
    # 导入为幂等 upsert：重复运行只更新题干参数，保留曝光数与启用状态。
    def wd_item(iid: str, discrimination: float) -> dict:
        return {
            "id": iid, "difficulty": 0.0, "discrimination": discrimination,
            "content_tag": "撤回演示", "stem": f"撤回演示题 {iid}：1 + 1 = ?",
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer": "A",
        }

    imported = call(
        "POST", "/api/items/import",
        {"items": [wd_item("DEMO-WD-1", 5.0), wd_item("DEMO-WD-2", 4.9),
                   wd_item("DEMO-WD-3", 4.8)]},
    )
    assert imported["valid"] is True
    # 重复运行时上一轮可能把演示题留在了撤回状态：先统一启用（演示启用路径）。
    for iid in ("DEMO-WD-1", "DEMO-WD-2", "DEMO-WD-3"):
        enabled = call("PATCH", f"/api/items/{iid}/availability",
                       {"is_active": True})
        assert enabled["is_active"] is True
    print("  -> 导入 3 道演示题并全部启用（重复导入保留启用状态与曝光数）✔")

    # 未知题目 PATCH -> 404
    expect_http_error(
        "PATCH", "/api/items/NO-SUCH-ITEM/availability", {"is_active": False},
    )

    # --- 已作答题目被撤回：已保存的 response_id 重放仍返回首次结果 ---
    sa = call("POST", "/api/sessions",
              {"max_items": 5, "exposure_limit": 10_000})
    sida = sa["id"]
    first = call("POST", f"/api/sessions/{sida}/next")
    assert first["item"]["id"] == "DEMO-WD-1"
    ans = call(
        "POST", f"/api/sessions/{sida}/responses",
        {"response_id": f"demo-wd-{sida[:8]}-1",
         "item_id": "DEMO-WD-1", "response": "A"},
    )
    withdrawn = call("PATCH", "/api/items/DEMO-WD-1/availability",
                     {"is_active": False})
    assert withdrawn["is_active"] is False
    replay = call(
        "POST", f"/api/sessions/{sida}/responses",
        {"response_id": f"demo-wd-{sida[:8]}-1",
         "item_id": "DEMO-WD-1", "response": "A"},
    )
    assert replay["created_at"] == ans["created_at"]
    assert replay["theta_after"] == ans["theta_after"]
    print("  -> 作答后撤回题目：已保存 response_id 重放仍返回首次结果（200）✔")

    # --- 挂起题被撤回：新作答 409；/next 同事务清除挂起项并重选，曝光不回退 ---
    sb = call("POST", "/api/sessions",
              {"max_items": 5, "exposure_limit": 10_000})
    sidb = sb["id"]
    pending = call("POST", f"/api/sessions/{sidb}/next")
    assert pending["item"]["id"] == "DEMO-WD-2"
    exposure_at_select = pending["exposure_count"]
    call("PATCH", "/api/items/DEMO-WD-2/availability", {"is_active": False})

    # 对已撤回题提交新作答 -> 409，不写答题记录、不更新能力
    expect_http_error(
        "POST", f"/api/sessions/{sidb}/responses",
        {"response_id": f"demo-wd-{sidb[:8]}-new",
         "item_id": "DEMO-WD-2", "response": "A"},
    )
    history = call("GET", f"/api/sessions/{sidb}/responses")
    assert history["count"] == 0, "撤回题的新作答不能写入答题记录"
    assert call("GET", f"/api/sessions/{sidb}")["theta"] == 0.0, "能力值不应更新"
    print("  -> 对撤回题提交新作答返回 409，未写答题记录、能力值不变 ✔")

    reselected = call("POST", f"/api/sessions/{sidb}/next")
    show("挂起题撤回后再次 /next（同事务清除挂起项并重选）", {
        "item": reselected["item"]["id"],
        "note": reselected["note"],
    })
    assert reselected["item"]["id"] == "DEMO-WD-3"
    assert "DEMO-WD-2" in (reselected["note"] or "")
    after = call("GET", "/api/items/DEMO-WD-2")
    assert after["exposure_count"] == exposure_at_select, \
        "撤回题已计曝光不应回退"
    print(f"  -> 重选出 DEMO-WD-3；DEMO-WD-2 曝光保持 {exposure_at_select} 不回退 ✔")

    # 收尾：把演示题留在撤回状态，保证重复运行本脚本时其他演示段落不受影响。
    for iid in ("DEMO-WD-1", "DEMO-WD-2", "DEMO-WD-3"):
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    bank = call("GET", "/api/items?limit=100")
    demo_rows = {i["id"]: i["is_active"] for i in bank["items"]
                 if i["id"].startswith("DEMO-WD-")}
    show("题库列表返回启用状态（演示题已重新撤回）", demo_rows)
    assert all(v is False for v in demo_rows.values())
    print("  -> 题库列表/详情均返回 is_active；演示题已撤回，不影响后续演示 ✔")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"无法连接 {BASE_URL}，请先启动服务（uvicorn app.main:app 或 docker compose up）。\n{exc}"
        )
