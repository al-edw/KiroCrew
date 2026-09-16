"""``GET /api/lessons`` names each row's ``repo_scope`` delete selector.

A lesson's identity is the pair ``(rule, repo_scope)``. ``DELETE /api/lessons``
has accepted a ``repo_scope`` selector since the CLI/MCP fix, but the list the
dashboard renders from carried no scope at all -- so two same-rule rows in two
scopes were indistinguishable duplicates in the Memory tab, and the only delete
the UI could send (no selector) removed both.

The list now answers, per row, the selector that names exactly that row under
the delete route's present-vs-absent semantics:

* ``""``  -- an unscoped (global) row: the route's explicit-global selector.
* fragment -- a scoped row, in canonical form, so it folds back onto the row.
* ``None`` -- a row whose stored scope is present but unusable. Both stores
  keep such a row reachable only through the UNSELECTIVE delete, and the route
  refuses the raw value as a selector, so the client must send none.

Each case is proven by ROUND-TRIPPING the emitted selector into the store's own
delete: it must remove that row and leave the same-rule sibling standing.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import cron
from kiro_crew.learn import LessonStore
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.asyncio

RULE = "run the gate before pushing"


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


async def _list(vector_store, state) -> list[dict]:
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.query = {}
    with (
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))),
        patch.object(cron, "_prepare_private_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vector_store)),
        patch.object(cron, "_get_active_workspace", return_value="default"),
    ):
        resp = await cron.api_lessons(request)
    assert resp.status == 200
    return json.loads(resp.text)["lessons"]


def _selectors(rows: list[dict]) -> dict[str | None, str]:
    """``repo_scope`` -> rule, asserting the key is PRESENT on every row."""
    out: dict[str | None, str] = {}
    for row in rows:
        assert "repo_scope" in row, row
        out[row["repo_scope"]] = row["rule"]
    return out


async def test_vector_rows_carry_the_selector_that_names_exactly_that_row(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        # Distinct rule text per row so the writer's dedup cannot merge them, but
        # a SHARED substring so the delete below is the ambiguous case the fix
        # is for: a substring that matches every row, told apart only by scope.
        assert store.write_lesson(f"{RULE} everywhere", "knowledge")
        # Trailing slash: the stored form is canonical, and the emitted selector
        # must be the canonical form too so the two fold identically.
        assert store.write_lesson(f"{RULE} in this repo", "knowledge", repo_scope="src/pkg/")
        # An imported row that bypassed the write surface with a scope the gate
        # can never satisfy: scoped-but-broken, not global.
        assert (
            store.set_semantic(
                "lesson.broken",
                {"rule": f"{RULE} nowhere", "category": "knowledge", "repo_scope": "/"},
                1.0,
                "user_explicit",
            )
            is None
        )

        rows = await _list(store, MagicMock())
        by_scope = _selectors(rows)
        assert by_scope == {
            "": f"{RULE} everywhere",
            "src/pkg": f"{RULE} in this repo",
            None: f"{RULE} nowhere",
        }

        # Round trip: the scoped row's selector removes that row ONLY, even
        # though the substring matches all three.
        assert store.delete_lesson(RULE, "src/pkg") is True
        remaining = _selectors(await _list(store, MagicMock()))
        assert set(remaining) == {"", None}, remaining

        # The global row's selector ("") removes the global row and leaves the
        # broken row, which only the unselective path may claim.
        assert store.delete_lesson(RULE, "") is True
        remaining = _selectors(await _list(store, MagicMock()))
        assert set(remaining) == {None}, remaining
    finally:
        store.close()


async def test_jsonl_rows_carry_the_same_selector_contract(tmp_path) -> None:
    lines = [
        {"ts": "2026-09-15T00:00:00+00:00", "rule": f"{RULE} everywhere", "category": "knowledge"},
        {
            "ts": "2026-09-15T00:00:01+00:00",
            "rule": f"{RULE} in this repo",
            "category": "knowledge",
            "repo_scope": "src/pkg/",
        },
        {
            "ts": "2026-09-15T00:00:02+00:00",
            "rule": f"{RULE} nowhere",
            "category": "knowledge",
            "repo_scope": "/",
        },
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    state = MagicMock()
    state.lessons = LessonStore(base_dir=tmp_path)

    by_scope = _selectors(await _list(None, state))
    assert by_scope == {
        "": f"{RULE} everywhere",
        "src/pkg": f"{RULE} in this repo",
        None: f"{RULE} nowhere",
    }

    # Round trip through the JSONL store's own remove: the scoped selector takes
    # its row and leaves the global sibling and the broken row.
    assert state.lessons.remove(RULE, "src/pkg") is True
    remaining = _selectors(await _list(None, state))
    assert set(remaining) == {"", None}, remaining


# The two other list surfaces read the same identity and feed the same
# scope-selective remove, so they render the scope under the same contract.


def test_mcp_learn_list_renders_the_scope_beside_the_rule() -> None:
    from kiro_crew.mcp_tools import learn

    rows = [
        {"rule": f"{RULE} everywhere", "category": "tool", "repo_scope": ""},
        {"rule": f"{RULE} in this repo", "category": "tool", "repo_scope": "src/pkg"},
        {"rule": f"{RULE} nowhere", "category": "tool", "repo_scope": None},
        # An older gateway that emits no scope at all renders as before.
        {"rule": f"{RULE} legacy", "category": "tool"},
    ]
    with patch.object(learn.mcp_core, "_get", return_value={"lessons": rows}):
        text = learn.learn_list("learn_list", {})
    assert text.splitlines() == [
        f"[tool] {RULE} everywhere",
        f"[tool] {RULE} in this repo (scope: src/pkg)",
        f"[tool] {RULE} nowhere (scope: unusable)",
        f"[tool] {RULE} legacy",
    ]


def test_cli_learn_list_renders_the_scope_for_both_tiers(tmp_path, capsys) -> None:
    import argparse

    from kiro_crew import cli_commands

    store = _store(tmp_path)
    try:
        assert store.write_lesson(f"{RULE} everywhere", "tool")
        assert store.write_lesson(f"{RULE} in this repo", "tool", repo_scope="src/pkg")
        assert (
            store.set_semantic(
                "lesson.broken",
                {"rule": f"{RULE} nowhere", "category": "tool", "repo_scope": "/"},
                1.0,
                "user_explicit",
            )
            is None
        )
        args = argparse.Namespace(learn_action="list")
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            cli_commands._learn(args)
    finally:
        store.close()
    out = capsys.readouterr().out
    assert f"[tool] {RULE} everywhere\n" in out
    assert f"[tool] {RULE} in this repo (scope: src/pkg)\n" in out
    assert f"[tool] {RULE} nowhere (scope: unusable)\n" in out

    # JSONL tier: the store the CLI falls back to when the vector tier is empty.
    lines = [
        {"ts": "t0", "rule": f"{RULE} everywhere", "category": "tool"},
        {"ts": "t1", "rule": f"{RULE} in this repo", "category": "tool", "repo_scope": "src/pkg"},
        {"ts": "t2", "rule": f"{RULE} nowhere", "category": "tool", "repo_scope": "/"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    empty_vs = MagicMock()
    empty_vs.get_lessons.return_value = []
    with (
        patch.object(cli_commands, "VectorMemoryStore", return_value=empty_vs),
        patch.object(cli_commands, "LessonStore", return_value=LessonStore(base_dir=tmp_path)),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
    ):
        cli_commands._learn(argparse.Namespace(learn_action="list"))
    out = capsys.readouterr().out
    assert f"[tool] {RULE} everywhere\n" in out
    assert f"[tool] {RULE} in this repo (scope: src/pkg)\n" in out
    assert f"[tool] {RULE} nowhere (scope: unusable)\n" in out


# A stored scope the credential redactor would alter is never echoed raw. The
# selector must round-trip byte-exact to name its row, so it cannot be emitted
# redacted either: the row is reported as unusable and only the unselective
# delete reaches it -- the same shape as a broken scope.
SECRET_SCOPE = "repos/ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234"


async def test_credential_shaped_scope_is_withheld_from_the_list(tmp_path) -> None:
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    # The fixture is only meaningful while the redactor actually alters it.
    assert _redact_memory_field(SECRET_SCOPE) != SECRET_SCOPE

    store = _store(tmp_path)
    try:
        assert store.write_lesson(f"{RULE} in a secret repo", "knowledge", repo_scope=SECRET_SCOPE)
        rows = await _list(store, MagicMock())
        assert len(rows) == 1
        assert rows[0]["repo_scope"] is None
        assert "ghp_" not in json.dumps(rows)
    finally:
        store.close()

    (tmp_path / "lessons.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-15T00:00:00+00:00",
                "rule": f"{RULE} in a secret repo",
                "category": "knowledge",
                "repo_scope": SECRET_SCOPE,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = MagicMock()
    state.lessons = LessonStore(base_dir=tmp_path)
    rows = await _list(None, state)
    assert len(rows) == 1
    assert rows[0]["repo_scope"] is None
    assert "ghp_" not in json.dumps(rows)


def test_cli_learn_list_prints_a_credential_shaped_scope_as_unusable(tmp_path, capsys) -> None:
    import argparse

    from kiro_crew import cli_commands

    store = _store(tmp_path)
    try:
        assert store.write_lesson(f"{RULE} in a secret repo", "tool", repo_scope=SECRET_SCOPE)
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            cli_commands._learn(argparse.Namespace(learn_action="list"))
    finally:
        store.close()
    out = capsys.readouterr().out
    assert f"[tool] {RULE} in a secret repo (scope: unusable)\n" in out
    assert "ghp_" not in out


def test_cli_learn_list_judges_the_scope_after_stripping_terminal_controls() -> None:
    """A credential split by an embedded escape sequence passes the redactor whole
    and would be reassembled by the control stripping the CLI applies before
    printing -- so the printed (stripped) text is what gets judged."""
    from kiro_crew import cli_commands
    from kiro_crew.security import redact

    split = "repos/ghp_ABCDEFGHIJKLMNOPQRST\x1b[0mUVWXYZabcdefghij1234"
    assert redact(split) == split, "fixture must pass the redactor unstripped"
    assert cli_commands._lesson_scope_suffix(split) == " (scope: unusable)"
    assert cli_commands._lesson_scope_suffix("src/pkg") == " (scope: src/pkg)"
    assert cli_commands._lesson_scope_suffix("src/\x1b[31mpkg") == " (scope: src/pkg)"
    assert cli_commands._lesson_scope_suffix("") == ""
    assert cli_commands._lesson_scope_suffix(None) == " (scope: unusable)"


# The JSONL list is a UNION of the global file and the active workspace's file,
# while the delete route picks a file from the body's ``scope`` / ``workspace``
# and defaults to the global one. Each row therefore names the tier it was read
# from, and a workspace row's delete carries it back -- otherwise the delete
# lands on the global file, removing a same-text global row and leaving this one.


async def test_jsonl_union_rows_name_their_tier_and_the_delete_honours_it(tmp_path) -> None:
    global_dir = tmp_path / "global"
    ws_dir = tmp_path / "ws"
    global_dir.mkdir()
    ws_dir.mkdir()
    (global_dir / "lessons.jsonl").write_text(
        json.dumps({"ts": "t0", "rule": f"{RULE} globally", "category": "tool"}) + "\n",
        encoding="utf-8",
    )
    (ws_dir / "lessons.jsonl").write_text(
        json.dumps({"ts": "t1", "rule": f"{RULE} in ws-1", "category": "tool"}) + "\n"
        # Same text as the global row: listed too, told apart by its tier --
        # a row the list hid was a row the UI could never delete.
        + json.dumps({"ts": "t2", "rule": f"{RULE} globally", "category": "tool"}) + "\n",
        encoding="utf-8",
    )
    global_store = LessonStore(base_dir=global_dir)
    ws_store = LessonStore(base_dir=ws_dir)
    state = MagicMock()
    state.lessons = global_store
    state.context_builder.get_lessons_for = MagicMock(return_value=ws_store)
    state.context_builder.memory.vector_store = None

    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.query = {}
    with (
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))),
        patch.object(cron, "_prepare_private_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
        patch.object(cron, "_get_active_workspace", return_value="ws-1"),
    ):
        resp = await cron.api_lessons(request)
    rows = json.loads(resp.text)["lessons"]
    assert [(row["rule"], row["scope"], row.get("workspace")) for row in rows] == [
        (f"{RULE} globally", "global", None),
        (f"{RULE} in ws-1", "workspace", "ws-1"),
        (f"{RULE} globally", "workspace", "ws-1"),
    ]
    state.context_builder.get_lessons_for.assert_called_with("ws-1")

    # Round trip: the workspace row's own selectors route its delete to the
    # workspace file; the global file is untouched. Same substring on both rows.
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    app["state"] = state
    app.router.add_route("*", "/api/lessons", cron.api_lessons_delete)
    with (
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "_sel"),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))),
        patch.object(cron, "_prepare_private_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.delete(
                "/api/lessons",
                json={"rule": RULE, "repo_scope": "", "scope": "workspace", "workspace": "ws-1"},
                headers={"X-Session-Key": "dashboard:ui"},
            )
            assert response.status == 200
            assert (await response.json())["ok"] is True
    assert [le.rule for le in ws_store.load_all()] == []
    assert [le.rule for le in global_store.load_all()] == [f"{RULE} globally"]


# ``exact`` narrows the rule match to the whole text. The default stays the
# substring the CLI and MCP callers rely on; a table row holding the full rule
# says ``exact`` so "use tabs" does not also take "always use tabs".


def test_vector_delete_exact_takes_only_the_whole_rule(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        # Two rules sharing the fragment "tabs" that the writer's dedup keeps
        # apart (neither contains the other, little topic overlap), so the
        # substring path reaches both while the exact path names one.
        assert store.write_lesson("prefer tabs", "preference")
        assert store.write_lesson("expand tabs when pasting into the terminal", "preference")
        assert len(store.get_lessons()) == 2
        # Exact: one row, case-insensitive, surrounding whitespace ignored.
        assert store.delete_lesson("  Prefer Tabs ", exact=True) is True
        remaining = [json.loads(e["value_json"])["rule"] for e in store.get_lessons()]
        assert remaining == ["expand tabs when pasting into the terminal"]
        # Exact with no whole-text match deletes nothing.
        assert store.delete_lesson("tabs", exact=True) is False
        assert len(store.get_lessons()) == 1
        # The default substring path still reaches the longer rule.
        assert store.delete_lesson("tabs") is True
        assert store.get_lessons() == []
    finally:
        store.close()


def test_jsonl_remove_exact_takes_only_the_whole_rule(tmp_path) -> None:
    lines = [
        {"ts": "t0", "rule": "use tabs", "category": "preference"},
        {"ts": "t1", "rule": "always use tabs in Makefiles", "category": "preference"},
        {"ts": "t2", "rule": "use tabs", "category": "preference", "repo_scope": "src/pkg"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    store = LessonStore(base_dir=tmp_path)
    # Exact + scope: the global "use tabs" only; the scoped twin and the longer
    # rule both stay.
    assert store.remove("  Use Tabs ", "", exact=True) is True
    assert [(le.rule, le.repo_scope) for le in store.load_all()] == [
        ("always use tabs in Makefiles", None),
        ("use tabs", "src/pkg"),
    ]
    assert store.remove("use tabs", "", exact=True) is False
    # Default substring path, unselective: everything containing the fragment.
    assert store.remove("use tabs") is True
    assert store.load_all() == []


async def test_delete_route_threads_exact_and_refuses_a_non_boolean() -> None:
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    app["state"] = SimpleNamespace(
        lessons=SimpleNamespace(remove=MagicMock(return_value=True)),
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=None)),
        push_refresh=MagicMock(),
    )
    app.router.add_route("*", "/api/lessons", cron.api_lessons_delete)
    with (
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "_sel"),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))),
        patch.object(cron, "_prepare_private_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
    ):
        async with TestClient(TestServer(app)) as client:
            headers = {"X-Session-Key": "dashboard:ui"}
            exact = await client.delete(
                "/api/lessons",
                json={"rule": "use tabs", "repo_scope": "", "exact": True},
                headers=headers,
            )
            assert exact.status == 200
            app["state"].lessons.remove.assert_called_once_with("use tabs", "", exact=True)

            app["state"].lessons.remove.reset_mock()
            default = await client.delete(
                "/api/lessons", json={"rule": "use tabs"}, headers=headers
            )
            assert default.status == 200
            app["state"].lessons.remove.assert_called_once_with("use tabs", None, exact=False)

            app["state"].lessons.remove.reset_mock()
            # A truthy STRING is not a boolean: "false" must not turn exact on,
            # and nothing must be deleted on a refused body.
            bad = await client.delete(
                "/api/lessons", json={"rule": "use tabs", "exact": "false"}, headers=headers
            )
            assert bad.status == 400
            assert (await bad.json())["code"] == "exact_not_bool"
            app["state"].lessons.remove.assert_not_called()
